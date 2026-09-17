'use client';

import {
  ArrowDownToLine, ArrowUpFromLine, Bot, Boxes, Building2,
  Check, ClipboardList, Download, FileText, History, LayoutDashboard,
  LogOut, Menu, Package, Plus, ReceiptText, Search, ShieldCheck,
  ScanBarcode, Settings2, TrendingDown, TrendingUp, TriangleAlert, X, XCircle,
} from 'lucide-react';
import { FormEvent, ReactNode, createContext, useCallback, useContext, useEffect, useId, useMemo, useState } from 'react';
import { ApiError, api, withReceipt } from './lib/api';
import type {
  AgentProposal, AgentRunResponse, AuditEventResponse, AuditResponse, ExpenseResponse, ItemResponse, MeResponse, MovementResponse,
  PricePolicyResponse, PurchaseResponse, ReplenishmentResponse, SupplierPriceIngestResponse, SupplierRecommendation, SupplierResponse,
} from './lib/types';
import {
  authenticate,
  beginPasswordReset,
  confirmEmployeeRegistration,
  finishPasswordReset,
  logout as cognitoLogout,
  registerEmployee,
} from './lib/auth';
import { dictionaries, Locale, localeNames } from './i18n';

type Role = 'admin' | 'supervisor' | 'employee';
type View = 'dashboard' | 'inventory' | 'activity' | 'procurement' | 'expenses' | 'audit' | 'people' | 'settings';
type Kind = 'inbound' | 'outbound';
type ExpenseStatus = 'submitted' | 'approved' | 'rejected' | 'paid';
type ThemeMode = 'light' | 'dark' | 'system';
type Accent = 'green' | 'blue' | 'orange';
type Density = 'comfortable' | 'compact';
type Appearance = { mode: ThemeMode; accent: Accent; density: Density };
type AppearancePreference = Appearance & { followCompanyDefault: boolean };
type AppearancePreferences = Record<string, AppearancePreference>;
type Item = { id: string; sku: string; name: string; category: string; location: string; qty: number; min: number; unit: string };
type Supplier = { id: string; name: string; contact: string; leadDays: number; rating: number; status: 'preferred' | 'backup' | 'paused' };
type Movement = { id: string; kind: Kind; itemId: string; qty: number; actor: string; recipient: string; note: string; at: string };
type Purchase = { id: string; itemId: string; supplierId: string; qty: number; unitCost: number; currency: string; receipt: string; receiptUrl?: string; invoice: string; at: string; priceChange: number };
type Expense = { id: string; submitter: string; itemId: string; supplier: string; qty: number; amount: number; currency: string; receipt: string; receiptUrl?: string; purpose: string; status: ExpenseStatus; at: string; reviewer?: string };
// quantity/priceChangePercent are demo-only stand-ins for the fields the real
// MongoDB audit-event payload carries, so the structured-search demo below has
// real fields to filter on instead of only prose.
type Audit = { id: string; actor: string; role: Role; action: string; target: string; detail: string; at: string; quantity?: number; priceChangePercent?: number };
type User = { email: string; password?: string; name: string; role: Role; scope: string };
type SupplierAdvice = { supplierId: string; supplierName: string; unitCost: number; currency: string; leadDays: number; rating: number; score: number };
type Replenishment = { itemId: string; itemName: string; sku: string; unit: string; quantityOnHand: number; dailyUsage: number; daysOfCover: number | null; suggestedQuantity: number; recommendedSupplier: SupplierAdvice | null; alternatives: SupplierAdvice[] };
type Store = { items: Item[]; suppliers: Supplier[]; movements: Movement[]; purchases: Purchase[]; expenses: Expense[]; audits: Audit[]; replenishment: Replenishment[]; threshold: number; defaultAppearance: Appearance };

const roleLabel: Record<Role, string> = { admin: 'Administrator', supervisor: 'Supervisor', employee: 'Employee' };
const roleKey: Record<Role, string> = { admin: 'administrator', supervisor: 'supervisor', employee: 'employee' };
const defaultAppearance: Appearance = { mode: 'system', accent: 'green', density: 'comfortable' };
const money = (value: number, currency = 'USD') => new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 2 }).format(value);
const date = (value: string) => new Intl.DateTimeFormat('en-CA', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));

type I18n = { locale: Locale; setLocale: (locale: Locale) => void; t: (key: string, fallback: string, values?: Record<string, string | number>) => string };
const I18nContext = createContext<I18n | null>(null);
function useI18n() { const value = useContext(I18nContext); if (!value) throw new Error('Missing i18n context'); return value; }
function LanguageSelect() { const { locale, setLocale, t } = useI18n(); return <label className="language-select"><span className="sr-only">{t('language', 'Language')}</span><select value={locale} onChange={event => setLocale(event.target.value as Locale)} aria-label={t('language', 'Language')}>{(Object.keys(localeNames) as Locale[]).map(option => <option key={option} value={option}>{localeNames[option]}</option>)}</select></label>; }

const seed: Store = {
  threshold: 15,
  defaultAppearance,
  replenishment: [],
  items: [
    { id: 'cable', sku: 'EL-CBL-001', name: 'USB-C charging cable', category: 'Electronics', location: 'A-01-02', qty: 86, min: 20, unit: 'pcs' },
    { id: 'paper', sku: 'ST-PPR-002', name: 'A4 copy paper', category: 'Office', location: 'B-03-01', qty: 12, min: 20, unit: 'reams' },
    { id: 'box', sku: 'PK-BX-004', name: 'Shipping box, medium', category: 'Packaging', location: 'C-02-04', qty: 240, min: 80, unit: 'pcs' },
    { id: 'mouse', sku: 'EL-MSE-012', name: 'Wireless mouse', category: 'Electronics', location: 'A-02-01', qty: 7, min: 12, unit: 'pcs' },
    { id: 'printer', sku: 'EL-PRN-020', name: 'Thermal label printer', category: 'Electronics', location: 'A-03-01', qty: 3, min: 5, unit: 'pcs' },
    { id: 'labels', sku: 'PK-LBL-013', name: '4 x 6 shipping labels', category: 'Packaging', location: 'C-04-02', qty: 9, min: 25, unit: 'rolls' },
    { id: 'wipes', sku: 'CL-WIP-010', name: 'Disinfecting wipes', category: 'Supplies', location: 'D-01-03', qty: 54, min: 20, unit: 'packs' },
    { id: 'stand', sku: 'EL-STD-008', name: 'Laptop stand', category: 'Electronics', location: 'A-04-01', qty: 18, min: 8, unit: 'pcs' },
  ],
  suppliers: [
    { id: 'northstar', name: 'Northstar Supply', contact: 'orders@northstar.example', leadDays: 3, rating: 4.8, status: 'preferred' },
    { id: 'atlas', name: 'Atlas Office Goods', contact: 'sales@atlas.example', leadDays: 5, rating: 4.4, status: 'backup' },
    { id: 'clearlane', name: 'ClearLane Distribution', contact: 'purchasing@clearlane.example', leadDays: 4, rating: 4.6, status: 'backup' },
    { id: 'metro', name: 'Metro Packaging Co.', contact: 'orders@metro.example', leadDays: 2, rating: 4.2, status: 'backup' },
  ],
  purchases: [
    { id: 'p3', itemId: 'cable', supplierId: 'northstar', qty: 100, unitCost: 3.6, currency: 'USD', invoice: 'INV-2086', receipt: 'northstar-invoice.pdf', at: '2026-08-25T08:15:00', priceChange: 20 },
    { id: 'p2', itemId: 'cable', supplierId: 'atlas', qty: 100, unitCost: 3, currency: 'USD', invoice: 'INV-1914', receipt: 'atlas-receipt.pdf', at: '2026-07-18T10:00:00', priceChange: -3.2 },
    { id: 'p1', itemId: 'cable', supplierId: 'northstar', qty: 100, unitCost: 3.1, currency: 'USD', invoice: 'INV-1760', receipt: 'northstar-june.pdf', at: '2026-06-12T09:30:00', priceChange: 0 },
    { id: 'p4', itemId: 'paper', supplierId: 'atlas', qty: 30, unitCost: 6.2, currency: 'USD', invoice: 'INV-2011', receipt: 'paper.pdf', at: '2026-08-10T09:30:00', priceChange: 4 },
    { id: 'p5', itemId: 'labels', supplierId: 'metro', qty: 40, unitCost: 12.5, currency: 'USD', invoice: 'MP-7781', receipt: 'metro-labels.pdf', at: '2026-08-22T14:10:00', priceChange: 18.4 },
    { id: 'p6', itemId: 'labels', supplierId: 'clearlane', qty: 40, unitCost: 10.55, currency: 'USD', invoice: 'CL-4408', receipt: 'clearlane-labels.pdf', at: '2026-07-08T11:25:00', priceChange: 0 },
    { id: 'p7', itemId: 'mouse', supplierId: 'northstar', qty: 20, unitCost: 21.4, currency: 'USD', invoice: 'NS-9033', receipt: 'northstar-mice.pdf', at: '2026-08-18T09:00:00', priceChange: -7.8 },
  ],
  movements: [
    { id: 'm1', kind: 'outbound', itemId: 'cable', qty: 4, actor: 'Alex Chen', recipient: 'Design team', note: 'New employee setup', at: '2026-08-25T09:42:00' },
    { id: 'm2', kind: 'inbound', itemId: 'box', qty: 120, actor: 'Mia Wong', recipient: 'Northstar Supply', note: 'PO-2026-0819', at: '2026-08-25T08:15:00' },
    { id: 'm3', kind: 'outbound', itemId: 'paper', qty: 6, actor: 'Alex Chen', recipient: 'Operations', note: 'Weekly office replenishment', at: '2026-08-24T16:20:00' },
    { id: 'm4', kind: 'outbound', itemId: 'labels', qty: 16, actor: 'Alex Chen', recipient: 'Shipping desk', note: 'Fulfillment run', at: '2026-08-23T13:30:00' },
    { id: 'm5', kind: 'inbound', itemId: 'wipes', qty: 36, actor: 'Mia Wong', recipient: 'ClearLane Distribution', note: 'PO-2026-0814', at: '2026-08-22T10:40:00' },
    { id: 'm6', kind: 'outbound', itemId: 'stand', qty: 3, actor: 'Alex Chen', recipient: 'Engineering team', note: 'Desk setup', at: '2026-08-21T15:05:00' },
  ],
  expenses: [
    { id: 'e1', submitter: 'Alex Chen', itemId: 'mouse', supplier: 'Local Tech Store', qty: 2, amount: 49.98, currency: 'USD', receipt: 'tech-store-receipt.jpg', purpose: 'Urgent replacement for meeting room', status: 'submitted', at: '2026-08-25T11:10:00' },
    { id: 'e2', submitter: 'Mia Wong', itemId: 'paper', supplier: 'Office Depot', qty: 4, amount: 24.8, currency: 'USD', receipt: 'office-depot.pdf', purpose: 'Urgent office restock', status: 'paid', reviewer: 'Chen Manager', at: '2026-08-21T14:40:00' },
    { id: 'e3', submitter: 'Alex Chen', itemId: 'labels', supplier: 'Corner Stationery', qty: 3, amount: 39.6, currency: 'USD', receipt: 'labels-august.jpg', purpose: 'Emergency shipping-label replacement', status: 'approved', reviewer: 'Mia Wong', at: '2026-08-23T09:20:00' },
    { id: 'e4', submitter: 'Alex Chen', itemId: 'wipes', supplier: 'City Mart', qty: 2, amount: 18.5, currency: 'USD', receipt: 'city-mart.jpg', purpose: 'Personal purchase not approved', status: 'rejected', reviewer: 'Mia Wong', at: '2026-08-19T17:15:00' },
  ],
  audits: [
    { id: 'a1', actor: 'Alex Chen', role: 'employee', action: 'Issued stock', target: 'USB-C charging cable', detail: '4 pcs issued to Design team', at: '2026-08-25T09:42:00', quantity: 4 },
    { id: 'a2', actor: 'Mia Wong', role: 'supervisor', action: 'Received purchase', target: 'Shipping box, medium', detail: '120 pcs from Northstar Supply', at: '2026-08-25T08:15:00', quantity: 120 },
    { id: 'a3', actor: 'Chen Manager', role: 'admin', action: 'Updated supplier', target: 'Northstar Supply', detail: 'Marked as preferred supplier', at: '2026-08-24T15:20:00' },
    { id: 'a4', actor: 'Mia Wong', role: 'supervisor', action: 'Approved reimbursement', target: '4 x 6 shipping labels', detail: 'Approved $39.60 submitted by Alex Chen', at: '2026-08-23T09:45:00', quantity: 39.6 },
    { id: 'a5', actor: 'Chen Manager', role: 'admin', action: 'Reviewed price alert', target: 'USB-C charging cable', detail: 'Northstar Supply price increased by 20%', at: '2026-08-25T08:30:00', priceChangePercent: 20 },
  ],
};

const demoAccounts: User[] = [
  { email: 'admin@stockroom.test', password: 'Stockroom!2026', name: 'Chen Manager', role: 'admin', scope: 'All warehouses and financial data' },
  { email: 'supervisor@stockroom.test', password: 'Stockroom!2026', name: 'Mia Wong', role: 'supervisor', scope: 'Warehouse operations and purchasing' },
  { email: 'employee@stockroom.test', password: 'Stockroom!2026', name: 'Alex Chen', role: 'employee', scope: 'Own stock issues and reimbursements' },
];
const localDemoAuth = process.env.NODE_ENV === 'development' && !process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID && !process.env.NEXT_PUBLIC_COGNITO_APP_CLIENT_ID;
const emptyStore = (): Store => ({ items: [], suppliers: [], movements: [], purchases: [], expenses: [], audits: [], replenishment: [], threshold: 15, defaultAppearance });
function demoReplenishment(store: Store): Replenishment[] {
  const recommendations = store.items.reduce<Replenishment[]>((result, item) => {
    const issued = store.movements.filter(movement => movement.kind === 'outbound' && movement.itemId === item.id).reduce((sum, movement) => sum + movement.qty, 0);
    const dailyUsage = issued ? Number((issued / 30).toFixed(2)) : item.qty <= item.min ? Number((item.min / 30).toFixed(2)) : 0;
    const daysOfCover = dailyUsage ? Number((item.qty / dailyUsage).toFixed(1)) : null;
    if (item.qty > item.min && (daysOfCover === null || daysOfCover > 21)) return result;
    const options = store.purchases.filter(purchase => purchase.itemId === item.id).map(purchase => {
      const supplier = store.suppliers.find(entry => entry.id === purchase.supplierId);
      return supplier ? { supplierId: supplier.id, supplierName: supplier.name, unitCost: purchase.unitCost, currency: purchase.currency, leadDays: supplier.leadDays, rating: supplier.rating, score: Math.round((purchase.unitCost ? 55 / purchase.unitCost : 0) + (14 - Math.min(14, supplier.leadDays)) * 2 + supplier.rating * 5) } : null;
    }).filter((option): option is SupplierAdvice => Boolean(option)).sort((a, b) => b.score - a.score || a.unitCost - b.unitCost);
    result.push({ itemId: item.id, itemName: item.name, sku: item.sku, unit: item.unit, quantityOnHand: item.qty, dailyUsage, daysOfCover, suggestedQuantity: Math.max(1, item.min * 2 - item.qty), recommendedSupplier: options[0] || null, alternatives: options });
    return result;
  }, []);
  return recommendations.sort((a, b) => (a.daysOfCover ?? 999) - (b.daysOfCover ?? 999));
}
const copyDemoStore = (): Store => { const value = JSON.parse(JSON.stringify(seed)) as Store; return { ...value, replenishment: demoReplenishment(value) }; };
const withDemoForecast = (store: Store): Store => ({ ...store, replenishment: demoReplenishment(store) });

function stored<T>(key: string, fallback: T): T { if (typeof window === 'undefined') return fallback; try { return JSON.parse(localStorage.getItem(key) || '') as T; } catch { return fallback; } }
function initials(name: string) { return name.split(' ').map(part => part[0]).join('').slice(0, 2); }
function isAppearancePreference(value: unknown): value is AppearancePreference { return Boolean(value && typeof value === 'object' && 'mode' in value && 'accent' in value && 'density' in value); }

export default function Home() {
  const [store, setStore] = useState<Store>(emptyStore);
  const [user, setUser] = useState<User | null>(null);
  // In local demo mode there is no session to fetch, so the app is ready on
  // the first render and the effect below has nothing to wait for.
  const [loading, setLoading] = useState(!localDemoAuth);
  const [view, setView] = useState<View>('dashboard');
  const [search, setSearch] = useState('');
  const [lowOnly, setLowOnly] = useState(false);
  const [dialog, setDialog] = useState<'issue' | 'receive' | 'purchase' | 'expense' | 'item' | null>(null);
  const [purchaseSeed, setPurchaseSeed] = useState<{ itemId: string; supplierId: string; quantity: number } | null>(null);
  const [menu, setMenu] = useState(false);
  const [notice, setNotice] = useState('');
  const [agentInstruction, setAgentInstruction] = useState('');
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentResult, setAgentResult] = useState<AgentRunResponse | null>(null);
  const [agentError, setAgentError] = useState('');
  const [auditEventFilters, setAuditEventFilters] = useState({ action: '', minQuantity: '', minPriceChangePercent: '' });
  const [auditEvents, setAuditEvents] = useState<AuditEventResponse[] | null>(null);
  const [auditEventsBusy, setAuditEventsBusy] = useState(false);
  const [ingestBusy, setIngestBusy] = useState(false);
  const [ingestResult, setIngestResult] = useState<SupplierPriceIngestResponse | null>(null);
  const [locale, setLocaleState] = useState<Locale>('en');
  const [appearancePreferences, setAppearancePreferences] = useState<AppearancePreferences>(() => {
    const saved = stored<unknown>('stockroom-appearance-v1', {});
    if (isAppearancePreference(saved)) return { legacy: saved };
    return saved && typeof saved === 'object' ? saved as AppearancePreferences : {};
  });
  useEffect(() => { localStorage.setItem('stockroom-appearance-v1', JSON.stringify(appearancePreferences)); }, [appearancePreferences]);
  // The stored locale is read after mount on purpose: localStorage is not
  // available while the server renders, so seeding state from it during the
  // first render would produce a hydration mismatch. This runs once and cannot
  // cascade.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { setLocaleState(stored<Locale>('stockroom-locale', 'en')); }, []);
  const refresh = useCallback(async () => {
    const current = await api<MeResponse>('/api/me');
    const nextUser: User = { ...current, scope: current.role === 'admin' ? 'All warehouses and financial data' : current.role === 'supervisor' ? 'Warehouse operations and purchasing' : 'Own stock issues and reimbursements' };
    const [items, movements, expenses] = await Promise.all([
      api<ItemResponse[]>('/api/items'), api<MovementResponse[]>('/api/movements'), api<ExpenseResponse[]>('/api/expenses'),
    ]);
    const restricted = current.role === 'employee';
    // Employees cannot read supplier, purchase, audit or replenishment data, so
    // the restricted branch supplies empty collections rather than calling
    // endpoints that would return 403.
    const restrictedDefaults: [SupplierResponse[], PurchaseResponse[], PricePolicyResponse, AuditResponse[], ReplenishmentResponse[]] =
      [[], [], { threshold_percent: 15 }, [], []];
    const [suppliers, purchases, policy, audits, replenishment] = restricted ? restrictedDefaults : await Promise.all([
      api<SupplierResponse[]>('/api/suppliers'),
      api<PurchaseResponse[]>('/api/purchases'),
      api<PricePolicyResponse>('/api/price-policy'),
      current.role === 'admin' ? api<AuditResponse[]>('/api/audit-logs') : Promise.resolve<AuditResponse[]>([]),
      api<ReplenishmentResponse[]>('/api/replenishment-recommendations'),
    ]);
    setUser(nextUser);
    setStore({
      items: items.map(item => ({ id: item.id, sku: item.sku, name: item.name, category: item.category, location: item.location, qty: item.quantity_on_hand, min: item.minimum_quantity, unit: item.unit })),
      suppliers: suppliers.map(supplier => ({ id: supplier.id, name: supplier.name, contact: supplier.contact, leadDays: supplier.lead_days, rating: supplier.rating, status: supplier.status })),
      movements: movements.map(movement => ({ id: movement.id, kind: movement.kind, itemId: movement.item_id, qty: movement.quantity, actor: movement.actor_name, recipient: movement.recipient, note: movement.note, at: movement.created_at })),
      purchases: purchases.map(purchase => ({ id: purchase.id, itemId: purchase.item_id, supplierId: purchase.supplier_id, qty: purchase.quantity, unitCost: Number(purchase.unit_cost), currency: purchase.currency, receipt: purchase.receipt_key || 'No attachment', invoice: purchase.invoice_number, at: purchase.created_at, priceChange: purchase.price_change_percent })),
      expenses: expenses.map(expense => ({ id: expense.id, submitter: expense.submitter_name, itemId: expense.item_id || '', supplier: expense.supplier, qty: expense.quantity, amount: Number(expense.amount), currency: expense.currency, receipt: expense.receipt_key || 'No attachment', purpose: expense.purpose, status: expense.status, at: expense.created_at, reviewer: expense.reviewer_name || undefined })),
      audits: audits.map(audit => ({ id: audit.id, actor: audit.actor_name, role: audit.actor_role, action: audit.action, target: audit.target_type, detail: audit.detail, at: audit.created_at })),
      replenishment: replenishment.map(recommendation => ({ itemId: recommendation.item_id, itemName: recommendation.item_name, sku: recommendation.sku, unit: recommendation.unit, quantityOnHand: recommendation.quantity_on_hand, dailyUsage: recommendation.daily_usage, daysOfCover: recommendation.days_of_cover, suggestedQuantity: recommendation.suggested_quantity, recommendedSupplier: recommendation.recommended_supplier ? { supplierId: recommendation.recommended_supplier.supplier_id, supplierName: recommendation.recommended_supplier.supplier_name, unitCost: Number(recommendation.recommended_supplier.unit_cost), currency: recommendation.recommended_supplier.currency, leadDays: recommendation.recommended_supplier.lead_days, rating: recommendation.recommended_supplier.rating, score: recommendation.recommended_supplier.score } : null, alternatives: recommendation.alternatives.map((option: SupplierRecommendation) => ({ supplierId: option.supplier_id, supplierName: option.supplier_name, unitCost: Number(option.unit_cost), currency: option.currency, leadDays: option.lead_days, rating: option.rating, score: option.score })) })),
      threshold: policy.threshold_percent,
      defaultAppearance,
    });
  }, []);
  // Loads the session and dashboard data once on mount. Every state update
  // inside `refresh` happens after an await, and the catch/finally handlers run
  // on promise resolution - none of it is the synchronous, cascading update the
  // rule guards against, which it cannot see through the async boundary.
  useEffect(() => {
    if (localDemoAuth) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh().catch(() => setUser(null)).finally(() => setLoading(false));
  }, [refresh]);
  const appearancePreference = user ? appearancePreferences[user.email] || { ...defaultAppearance, followCompanyDefault: true } : { ...defaultAppearance, followCompanyDefault: true };
  const updateAppearancePreference = (next: AppearancePreference | ((current: AppearancePreference) => AppearancePreference)) => {
    if (!user) return;
    setAppearancePreferences(current => {
      const prior = current[user.email] || { ...defaultAppearance, followCompanyDefault: true };
      return { ...current, [user.email]: typeof next === 'function' ? next(prior) : next };
    });
  };
  const appearance = appearancePreference.followCompanyDefault ? store.defaultAppearance : appearancePreference;
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => {
      document.documentElement.dataset.theme = appearance.mode === 'system' ? (media.matches ? 'dark' : 'light') : appearance.mode;
      document.documentElement.dataset.accent = appearance.accent;
      document.documentElement.dataset.density = appearance.density;
    };
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [appearance]);
  const tell = (message: string) => { setNotice(message); window.setTimeout(() => setNotice(''), 2800); };
  const audit = (actor: User, action: string, target: string, detail: string) => ({ id: crypto.randomUUID(), actor: actor.name, role: actor.role, action, target, detail, at: new Date().toISOString() });
  const canManage = user?.role === 'admin';
  const canApprove = user?.role === 'admin' || user?.role === 'supervisor';
  const canReceive = user?.role !== 'employee';
  const canAudit = user?.role === 'admin';
  const canSeeActivity = user?.role !== 'employee';
  const visibleMoves = localDemoAuth && user?.role === 'employee' ? store.movements.filter(move => move.actor === user.name) : store.movements;
  const visibleExpenses = localDemoAuth && user?.role === 'employee' ? store.expenses.filter(expense => expense.submitter === user.name) : store.expenses;
  const items = useMemo(() => store.items.filter(item => `${item.name} ${item.sku} ${item.category} ${item.location}`.toLowerCase().includes(search.toLowerCase())).filter(item => !lowOnly || item.qty <= item.min), [store.items, search, lowOnly]);
  const lowStock = store.items.filter(item => item.qty <= item.min);
  const priceAlerts = store.purchases.filter(purchase => purchase.priceChange >= store.threshold);
  const navigateInventory = (low = false) => { setLowOnly(low); setView('inventory'); };
  const logout = async () => { if (!localDemoAuth) await cognitoLogout(); setUser(null); setStore(emptyStore()); setView('dashboard'); };
  const login = async (email: string, password: string) => {
    if (localDemoAuth) {
      const account = demoAccounts.find(entry => entry.email === email && entry.password === password);
      if (!account) throw new Error('Use one of the local demo accounts shown below.');
      setUser(account); setStore(copyDemoStore()); return;
    }
    await authenticate(email, password); await refresh();
  };
  const setLocale = (nextLocale: Locale) => { setLocaleState(nextLocale); localStorage.setItem('stockroom-locale', nextLocale); };
  const t = (key: string, fallback: string, values?: Record<string, string | number>) => { let text = locale === 'en' ? fallback : (dictionaries[locale][key] || fallback); Object.entries(values || {}).forEach(([name, value]) => { text = text.replace(`{${name}}`, String(value)); }); return text; };
  const i18n = { locale, setLocale, t };
  const addMovement = async (event: FormEvent<HTMLFormElement>, kind: Kind) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      if (localDemoAuth && user) {
        const itemId = String(form.get('item'));
        const quantity = Number(form.get('qty'));
        const item = store.items.find(entry => entry.id === itemId);
        if (!item) throw new Error('Select an inventory item.');
        if (kind === 'outbound' && quantity > item.qty) throw new Error('Insufficient stock for this issue.');
        const movement: Movement = { id: crypto.randomUUID(), kind, itemId, qty: quantity, actor: user.name, recipient: String(form.get('recipient')).trim(), note: String(form.get('note')).trim(), at: new Date().toISOString() };
        setStore(value => withDemoForecast({ ...value, items: value.items.map(entry => entry.id === itemId ? { ...entry, qty: entry.qty + (kind === 'inbound' ? quantity : -quantity) } : entry), movements: [movement, ...value.movements], audits: [audit(user, kind === 'inbound' ? 'Received stock' : 'Issued stock', item.name, `${quantity} ${item.unit} ${kind === 'inbound' ? 'received' : 'issued'} to ${movement.recipient}`), ...value.audits] }));
        setDialog(null); tell(kind === 'inbound' ? 'Stock receipt recorded.' : 'Stock issue recorded.'); return;
      }
      await api('/api/movements', { method: 'POST', body: JSON.stringify({ item_id: String(form.get('item')), kind, quantity: Number(form.get('qty')), recipient: String(form.get('recipient')).trim(), note: String(form.get('note')).trim() }) });
      await refresh(); setDialog(null); tell(kind === 'inbound' ? 'Stock receipt recorded.' : 'Stock issue recorded.');
    } catch (error) { tell(error instanceof Error ? error.message : 'Stock movement could not be recorded.'); }
  };
  const addItem = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const name = String(form.get('name')).trim();
      if (localDemoAuth && user) {
        const item: Item = { id: crypto.randomUUID(), name, sku: String(form.get('sku')).trim() || `SKU-${Date.now().toString().slice(-5)}`, category: String(form.get('category')).trim() || 'Uncategorized', location: String(form.get('location')).trim() || 'Unassigned', qty: Number(form.get('qty')) || 0, min: Number(form.get('min')) || 0, unit: String(form.get('unit')).trim() || 'pcs' };
        setStore(value => withDemoForecast({ ...value, items: [item, ...value.items], audits: [audit(user, 'Created inventory item', item.name, `Starting quantity: ${item.qty} ${item.unit}`), ...value.audits] }));
        setDialog(null); tell(`${name} added.`); return;
      }
      await api('/api/items', { method: 'POST', body: JSON.stringify({ name, sku: String(form.get('sku')).trim() || `SKU-${Date.now().toString().slice(-5)}`, category: String(form.get('category')).trim() || 'Uncategorized', location: String(form.get('location')).trim() || 'Unassigned', quantity_on_hand: Number(form.get('qty')) || 0, minimum_quantity: Number(form.get('min')) || 0, unit: String(form.get('unit')).trim() || 'pcs' }) });
      await refresh(); setDialog(null); tell(`${name} added.`);
    } catch (error) { tell(error instanceof Error ? error.message : 'Item could not be created.'); }
  };
  const addPurchase = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget); const receipt = form.get('receipt') as File;
    try {
      if (localDemoAuth && user) {
        const itemId = String(form.get('item')); const supplierId = String(form.get('supplier')); const quantity = Number(form.get('qty')); const unitCost = Number(form.get('unitCost'));
        const previous = store.purchases.find(purchase => purchase.itemId === itemId);
        const item = store.items.find(entry => entry.id === itemId);
        if (!item) throw new Error('Select an inventory item.');
        const purchase: Purchase = { id: crypto.randomUUID(), itemId, supplierId, qty: quantity, unitCost, currency: 'USD', invoice: String(form.get('invoice')).trim(), receipt: receipt?.name || 'No attachment', at: new Date().toISOString(), priceChange: previous ? Number((((unitCost - previous.unitCost) / previous.unitCost) * 100).toFixed(1)) : 0 };
        setStore(value => withDemoForecast({ ...value, items: value.items.map(entry => entry.id === itemId ? { ...entry, qty: entry.qty + quantity } : entry), purchases: [purchase, ...value.purchases], movements: [{ id: crypto.randomUUID(), kind: 'inbound', itemId, qty: quantity, actor: user.name, recipient: value.suppliers.find(entry => entry.id === supplierId)?.name || 'Supplier', note: purchase.invoice, at: purchase.at }, ...value.movements], audits: [audit(user, 'Received purchase', item.name, `${quantity} ${item.unit} received at ${money(unitCost)} each`), ...value.audits] }));
        setDialog(null); tell('Purchase and receipt recorded.'); return;
      }
      await withReceipt(receipt, uploaded => api('/api/purchases', { method: 'POST', body: JSON.stringify({ item_id: String(form.get('item')), supplier_id: String(form.get('supplier')), quantity: Number(form.get('qty')), unit_cost: Number(form.get('unitCost')), currency: 'USD', invoice_number: String(form.get('invoice')).trim(), receipt_key: uploaded?.key, receipt_version_id: uploaded?.versionId }) }));
      await refresh(); setDialog(null); tell('Purchase and receipt recorded.');
    } catch (error) { tell(error instanceof Error ? error.message : 'Purchase could not be recorded.'); }
  };
  const addExpense = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget); const receipt = form.get('receipt') as File;
    try {
      if (localDemoAuth && user) {
        const expense: Expense = { id: crypto.randomUUID(), submitter: user.name, itemId: String(form.get('item')) || '', supplier: String(form.get('supplier')).trim(), qty: Number(form.get('qty')), amount: Number(form.get('amount')), currency: 'USD', receipt: receipt?.name || 'No attachment', purpose: String(form.get('purpose')).trim(), status: 'submitted', at: new Date().toISOString() };
        setStore(value => ({ ...value, expenses: [expense, ...value.expenses], audits: [audit(user, 'Submitted reimbursement', expense.supplier, `${money(expense.amount)} reimbursement submitted`), ...value.audits] }));
        setDialog(null); tell('Reimbursement submitted for approval.'); return;
      }
      await withReceipt(receipt, uploaded => api('/api/expenses', { method: 'POST', body: JSON.stringify({ item_id: String(form.get('item')) || null, supplier: String(form.get('supplier')).trim(), quantity: Number(form.get('qty')), amount: Number(form.get('amount')), currency: 'USD', purpose: String(form.get('purpose')).trim(), receipt_key: uploaded?.key, receipt_version_id: uploaded?.versionId }) }));
      await refresh(); setDialog(null); tell('Reimbursement submitted for approval.');
    } catch (error) { tell(error instanceof Error ? error.message : 'Expense could not be submitted.'); }
  };
  const updateExpense = async (expense: Expense, status: ExpenseStatus) => {
    try {
      if (localDemoAuth && user) {
        setStore(value => ({ ...value, expenses: value.expenses.map(entry => entry.id === expense.id ? { ...entry, status, reviewer: user.name } : entry), audits: [audit(user, `Marked reimbursement ${status}`, expense.supplier, `${money(expense.amount)} reimbursement marked ${status}`), ...value.audits] }));
        tell(`Expense marked ${status}.`); return;
      }
      await api(`/api/expenses/${expense.id}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }); await refresh(); tell(`Expense marked ${status}.`);
    }
    catch (error) { tell(error instanceof Error ? error.message : 'Expense status could not be updated.'); }
  };
  const updatePriceThreshold = async (value: number) => {
    try {
      if (localDemoAuth && user) { const threshold = Math.max(1, Math.round(value)); setStore(current => ({ ...current, threshold, audits: [audit(user, 'Updated price alert policy', 'Price policy', `Price increase threshold: ${threshold}%`), ...current.audits] })); tell('Price alert policy updated.'); return; }
      await api('/api/price-policy', { method: 'PATCH', body: JSON.stringify({ threshold_percent: Math.max(1, Math.round(value)) }) }); await refresh(); tell('Price alert policy updated.');
    }
    catch (error) { tell(error instanceof Error ? error.message : 'Price policy could not be updated.'); }
  };
  const updateCompanyAppearance = (next: Appearance) => {
    if (!user || !canManage) return;
    const current = store.defaultAppearance;
    if (current.mode === next.mode && current.accent === next.accent && current.density === next.density) return;
    setStore(value => ({ ...value, defaultAppearance: next, audits: [audit(user, 'Updated company appearance', 'Settings', `Default theme: ${next.mode}; accent: ${next.accent}; density: ${next.density}`), ...value.audits] }));
    tell('Company default appearance updated.');
  };
  const openPurchase = (seed: { itemId: string; supplierId: string; quantity: number } | null = null) => { setPurchaseSeed(seed); setDialog('purchase'); };
  const approveProposal = (proposal: AgentProposal) => openPurchase({ itemId: proposal.item_id, supplierId: proposal.supplier_id, quantity: proposal.quantity });
  const runAgent = async () => {
    setAgentBusy(true); setAgentError(''); setAgentResult(null);
    try {
      if (localDemoAuth) {
        // There is no model to call in the local demo, but the tools the real
        // agent uses just read replenishment_recommendations - which is
        // already computed right here - so the demo proposes from the same
        // numbers rather than inventing a plausible-looking response.
        await new Promise(resolve => window.setTimeout(resolve, 500));
        const targets = store.replenishment.filter(entry => entry.recommendedSupplier).slice(0, 3);
        const proposals: AgentProposal[] = targets.map(entry => ({
          sku: entry.sku, item_name: entry.itemName, item_id: entry.itemId,
          supplier_name: entry.recommendedSupplier!.supplierName, supplier_id: entry.recommendedSupplier!.supplierId,
          quantity: entry.suggestedQuantity, unit: entry.unit,
          reason: entry.daysOfCover === null ? `${entry.itemName} is at or below its minimum level.` : `${entry.daysOfCover} days of cover left at ${entry.dailyUsage} ${entry.unit}/day.`,
          engine_suggested_quantity: entry.suggestedQuantity, differs_from_engine: false, requires_human_approval: true,
        }));
        setAgentResult({
          summary: proposals.length ? `Proposed ${proposals.length} purchase${proposals.length === 1 ? '' : 's'} from the current replenishment guidance.` : 'Nothing needs ordering right now.',
          proposals,
          steps: [
            { iteration: 0, tool: 'replenishment_needs', arguments: {}, ok: true },
            ...proposals.map((proposal, index) => ({ iteration: index + 1, tool: 'propose_purchase', arguments: { sku: proposal.sku, supplier_name: proposal.supplier_name, quantity: proposal.quantity }, ok: true })),
          ],
          stopped_because: 'completed', duration_ms: 480,
        });
        return;
      }
      const result = await api<AgentRunResponse>('/api/agent/replenishment', { method: 'POST', body: JSON.stringify({ instruction: agentInstruction || undefined }) });
      setAgentResult(result);
    } catch (error) {
      setAgentError(error instanceof ApiError && error.status === 503 ? 'No AI provider is configured on the server. Set AGENT_PROVIDER in backend/.env to enable this.' : error instanceof Error ? error.message : 'The agent could not complete this run.');
    } finally { setAgentBusy(false); }
  };
  const searchAuditEvents = async () => {
    setAuditEventsBusy(true);
    try {
      if (localDemoAuth) {
        await new Promise(resolve => window.setTimeout(resolve, 250));
        // Local demo mode has no MongoDB behind it, so this filters the same
        // in-memory audit trail on the fields the real payload carries,
        // rather than only on the prose `detail` sentence.
        const minQuantity = Number(auditEventFilters.minQuantity) || 0;
        const minPct = Number(auditEventFilters.minPriceChangePercent) || 0;
        const matches = store.audits.filter(entry => {
          if (auditEventFilters.action && !entry.action.toLowerCase().includes(auditEventFilters.action.toLowerCase())) return false;
          if (minQuantity && !(entry.quantity !== undefined && entry.quantity >= minQuantity)) return false;
          if (minPct && !(entry.priceChangePercent !== undefined && entry.priceChangePercent >= minPct)) return false;
          return true;
        });
        setAuditEvents(matches.map(entry => ({
          id: entry.id, actor: { id: entry.id, role: entry.role, name: entry.actor }, action: entry.action,
          target: { type: 'item', id: entry.id }, detail: entry.detail,
          payload: { ...(entry.quantity !== undefined ? { quantity: entry.quantity } : {}), ...(entry.priceChangePercent !== undefined ? { price_change_percent: entry.priceChangePercent } : {}) },
          created_at: entry.at,
        })));
        return;
      }
      const params = new URLSearchParams();
      if (auditEventFilters.action) params.set('action', auditEventFilters.action);
      if (auditEventFilters.minQuantity) params.set('min_quantity', auditEventFilters.minQuantity);
      if (auditEventFilters.minPriceChangePercent) params.set('min_price_change_percent', auditEventFilters.minPriceChangePercent);
      const query = params.toString();
      setAuditEvents(await api<AuditEventResponse[]>(`/api/audit-events${query ? `?${query}` : ''}`));
    } catch (error) { tell(error instanceof Error ? error.message : 'Could not search structured audit events.'); }
    finally { setAuditEventsBusy(false); }
  };
  const runSupplierIngest = async () => {
    setIngestBusy(true);
    try {
      const result = await api<SupplierPriceIngestResponse>('/api/supplier-prices/ingest', { method: 'POST' });
      setIngestResult(result); tell(`Checked supplier submissions: ${result.processed} processed.`);
    } catch (error) { tell(error instanceof Error ? error.message : 'Could not check supplier submissions.'); }
    finally { setIngestBusy(false); }
  };
  const exportAudits = () => { const rows = store.audits.map(entry => [date(entry.at), entry.actor, roleLabel[entry.role], entry.action, entry.target, entry.detail].map(value => `"${String(value).replaceAll('"', '""')}"`).join(',')); const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([['Time,Actor,Role,Action,Target,Detail', ...rows].join('\n')], { type: 'text/csv' })); link.download = 'stockroom-audit-log.csv'; link.click(); URL.revokeObjectURL(link.href); };
  if (loading) return <I18nContext.Provider value={i18n}><main className="login-page"><div className="login-card">Loading secure workspace...</div></main></I18nContext.Provider>;
  if (!user) return <I18nContext.Provider value={i18n}><LocalizedLogin onLogin={login} demoAccounts={localDemoAuth ? demoAccounts : undefined} /></I18nContext.Provider>;
  const nav: { view: View; label: string; icon: typeof LayoutDashboard; visible: boolean }[] = [
    { view: 'dashboard', label: t('overview', 'Overview'), icon: LayoutDashboard, visible: true }, { view: 'inventory', label: t('inventory', 'Inventory'), icon: Boxes, visible: true }, { view: 'activity', label: t('activity', 'Activity'), icon: ClipboardList, visible: canSeeActivity }, { view: 'procurement', label: t('procurement', 'Procurement'), icon: Building2, visible: canReceive }, { view: 'expenses', label: t('reimbursements', 'Reimbursements'), icon: ReceiptText, visible: true }, { view: 'audit', label: t('auditLog', 'Audit log'), icon: History, visible: canAudit }, { view: 'settings', label: t('settings', 'Settings'), icon: Settings2, visible: true },
  ];
  return <I18nContext.Provider value={i18n}><main className="app-shell">
    <aside className={`sidebar ${menu ? 'sidebar-open' : ''}`}><div className="brand"><span className="brand-mark"><Package size={19} /></span><span><b>Stockroom</b><small>Warehouse operations</small></span></div><nav>{nav.filter(entry => entry.visible).map(entry => { const Icon = entry.icon; return <button key={entry.view} onClick={() => { setView(entry.view); setMenu(false); }} className={view === entry.view ? 'nav-active' : ''}><Icon size={18} />{entry.label}</button>; })}</nav><div className="sidebar-note"><b>{t(roleKey[user.role], roleLabel[user.role])}</b><span>{user.scope}</span></div></aside>
    {menu && <button aria-label="Close navigation" onClick={() => setMenu(false)} className="scrim" />}
    <section className="content"><header><div className="title-row"><IconButton label="Open navigation" onClick={() => setMenu(true)}><Menu size={18} /></IconButton><div><h1>{nav.find(entry => entry.view === view)?.label}</h1><p>Warehouse 01</p></div></div><div className="header-actions"><LanguageSelect />{canAudit && <IconButton label={t('exportAudit', 'Export audit log')} onClick={exportAudits}><Download size={17} /></IconButton>}<div className="avatar">{initials(user.name)}</div><div className="account-name"><b>{user.name}</b><small>{t(roleKey[user.role], roleLabel[user.role])}</small></div><IconButton label={t('signOut', 'Sign out')} onClick={logout}><LogOut size={17} /></IconButton></div></header>
      <div className="page"><div className="intro"><div><p>Warehouse 01 · {t(roleKey[user.role], roleLabel[user.role])}</p><h2>{view === 'dashboard' ? t('welcome', 'Welcome back, {name}.', { name: user.name }) : nav.find(entry => entry.view === view)?.label}</h2></div><div className="commands"><button className="secondary" onClick={() => setDialog('issue')}><ArrowUpFromLine size={17} />{t('issueStock', 'Issue stock')}</button>{canReceive && <button className="primary" onClick={() => openPurchase()}><ArrowDownToLine size={17} />{t('receivePurchase', 'Receive purchase')}</button>}</div></div>
        {view === 'dashboard' && <Dashboard items={store.items} movements={visibleMoves} lowStock={lowStock} priceAlerts={priceAlerts} replenishment={store.replenishment} goInventory={navigateInventory} goProcurement={() => setView('procurement')} goActivity={() => setView('activity')} />}
        {view === 'inventory' && <Inventory items={items} search={search} setSearch={setSearch} lowOnly={lowOnly} clearLow={() => setLowOnly(false)} canManage={canManage} add={() => setDialog('item')} />}
        {view === 'activity' && canSeeActivity && <Activity movements={visibleMoves} items={store.items} />}
        {view === 'procurement' && canReceive && <Procurement purchases={store.purchases} suppliers={store.suppliers} items={store.items} alerts={priceAlerts} replenishment={store.replenishment} threshold={store.threshold} canManage={canManage} setThreshold={updatePriceThreshold} add={() => openPurchase()} agentInstruction={agentInstruction} setAgentInstruction={setAgentInstruction} agentBusy={agentBusy} agentResult={agentResult} agentError={agentError} runAgent={runAgent} approveProposal={approveProposal} showIngest={!localDemoAuth && canManage} ingestBusy={ingestBusy} ingestResult={ingestResult} runSupplierIngest={runSupplierIngest} />}
        {view === 'expenses' && <Expenses expenses={visibleExpenses} items={store.items} canApprove={canApprove} canPay={canManage} add={() => setDialog('expense')} update={updateExpense} />}
        {view === 'audit' && canAudit && <AuditLog audits={store.audits} eventFilters={auditEventFilters} setEventFilters={setAuditEventFilters} events={auditEvents} eventsBusy={auditEventsBusy} search={searchAuditEvents} />}
        {view === 'settings' && <AppearanceSettings preference={appearancePreference} companyDefault={store.defaultAppearance} canManage={canManage} updatePreference={updateAppearancePreference} updateCompanyDefault={updateCompanyAppearance} />}
      </div>
    </section>
    {notice && <div role="status" className="toast">{notice}</div>}
    {dialog === 'issue' && <MovementDialog title="Issue stock" kind="outbound" items={store.items} close={() => setDialog(null)} submit={addMovement} />}
    {dialog === 'receive' && <MovementDialog title="Receive stock" kind="inbound" items={store.items} close={() => setDialog(null)} submit={addMovement} />}
    {dialog === 'purchase' && <PurchaseDialog items={store.items} suppliers={store.suppliers} recommendations={store.replenishment} seed={purchaseSeed} close={() => { setDialog(null); setPurchaseSeed(null); }} submit={addPurchase} />}
    {dialog === 'expense' && <ExpenseDialog items={store.items} close={() => setDialog(null)} submit={addExpense} />}
    {dialog === 'item' && <ItemDialog close={() => setDialog(null)} submit={addItem} />}
  </main></I18nContext.Provider>;
}

function Dashboard({ items, movements, lowStock, priceAlerts, replenishment, goInventory, goProcurement, goActivity }: { items: Item[]; movements: Movement[]; lowStock: Item[]; priceAlerts: Purchase[]; replenishment: Replenishment[]; goInventory: (low?: boolean) => void; goProcurement: () => void; goActivity: () => void }) {
  const units = items.reduce((sum, item) => sum + item.qty, 0); const cards = [
    ['Stocked items', String(items.length), 'Active SKUs', Boxes, 'green', () => goInventory()], ['Units on hand', units.toLocaleString(), 'Across all locations', Package, 'blue', () => goInventory()], ['Needs attention', String(lowStock.length), 'Below minimum level', TriangleAlert, 'orange', () => goInventory(true)], ['Price alerts', String(priceAlerts.length), 'Supplier review needed', TrendingUp, 'violet', goProcurement],
  ] as const;
  return <><div className="metric-grid">{cards.map(([title, value, detail, Icon, tone, click]) => <button className="metric metric-button" key={title} type="button" onClick={click}><div><p>{title}</p><strong>{value}</strong><small>{detail}</small></div><span className={`metric-icon ${tone}`}><Icon size={18} /></span></button>)}</div><div className="dashboard-grid"><section className="panel"><PanelHeading title="Recent activity" note="Latest warehouse movements" action="View activity" onClick={goActivity} />{movements.slice(0, 5).map(move => <MovementRow key={move.id} movement={move} item={items.find(item => item.id === move.itemId)} />)}{!movements.length && <Empty label="No movements in your visible scope." />}</section><section className="panel"><PanelHeading title="Attention queue" note="Stock and price issues requiring review" />{lowStock.slice(0, 2).map(item => <div className="low-row" key={item.id}><div><b>{item.name}</b><small>Low stock · minimum {item.min} {item.unit}</small></div><em>{item.qty} {item.unit}</em></div>)}{priceAlerts.slice(0, 2).map(purchase => { const item = items.find(entry => entry.id === purchase.itemId); return <div className="low-row" key={purchase.id}><div><b>{item?.name}</b><small>Purchase price increased {purchase.priceChange}%</small></div><em>{money(purchase.unitCost)}</em></div>; })}{!lowStock.length && !priceAlerts.length && <Empty label="Everything is within its configured limits." />}</section></div>{replenishment.length > 0 && <section className="panel forecast-panel"><PanelHeading title="Replenishment forecast" note="Based on outbound demand, current stock, supplier price, and lead time" action="Review purchase options" onClick={goProcurement} />{replenishment.slice(0, 3).map(recommendation => <div className="forecast-row" key={recommendation.itemId}><div><b>{recommendation.itemName}</b><small>{recommendation.daysOfCover === null ? 'Low stock with no recent outbound history' : `${recommendation.daysOfCover} days of cover at ${recommendation.dailyUsage} ${recommendation.unit}/day`}</small></div><div><strong>Order {recommendation.suggestedQuantity} {recommendation.unit}</strong><small>{recommendation.recommendedSupplier ? `${recommendation.recommendedSupplier.supplierName} · ${money(recommendation.recommendedSupplier.unitCost, recommendation.recommendedSupplier.currency)} · ${recommendation.recommendedSupplier.leadDays} days` : 'Add supplier price history to compare options'}</small></div></div>)}</section>}</>;
}

function Inventory({ items, search, setSearch, lowOnly, clearLow, canManage, add }: { items: Item[]; search: string; setSearch: (value: string) => void; lowOnly: boolean; clearLow: () => void; canManage: boolean; add: () => void }) { return <section className="panel"><div className="table-tools"><label><Search size={17} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search item, SKU, category, or location" /></label><div className="table-actions">{lowOnly && <button className="filter-pill" onClick={clearLow}>Low stock only <X size={14} /></button>}{canManage && <button className="primary" onClick={add}><Plus size={17} />New item</button>}</div></div><div className="table-wrap"><table><thead><tr><th>Item</th><th>Location</th><th>On hand</th><th>Minimum</th><th>Status</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td><b>{item.name}</b><small className="mono">{item.sku} · {item.category}</small></td><td>{item.location}</td><td><b>{item.qty}</b> <span>{item.unit}</span></td><td>{item.min} {item.unit}</td><td><Status tone={item.qty <= item.min ? 'warn' : 'good'}>{item.qty <= item.min ? 'Low stock' : 'Healthy'}</Status></td></tr>)}{!items.length && <tr><td colSpan={5}><Empty label="No inventory matches this filter." /></td></tr>}</tbody></table></div></section>; }

function Activity({ movements, items }: { movements: Movement[]; items: Item[] }) { return <section className="panel"><PanelHeading title="Warehouse activity" note="Receipts and issues in your visible scope" /><div className="table-wrap"><table><thead><tr><th>Time</th><th>Movement</th><th>Item</th><th>Quantity</th><th>Handled by</th><th>Recipient / supplier</th></tr></thead><tbody>{movements.map(move => { const item = items.find(entry => entry.id === move.itemId); return <tr key={move.id}><td>{date(move.at)}</td><td><Status tone={move.kind === 'inbound' ? 'good' : 'warn'}>{move.kind === 'inbound' ? 'Receipt' : 'Issue'}</Status></td><td><b>{item?.name || 'Deleted item'}</b><small>{move.note}</small></td><td><b className={move.kind === 'inbound' ? 'amount-in' : 'amount-out'}>{move.kind === 'inbound' ? '+' : '-'}{move.qty} {item?.unit}</b></td><td>{move.actor}</td><td>{move.recipient}</td></tr>; })}{!movements.length && <tr><td colSpan={6}><Empty label="No activity in your visible scope." /></td></tr>}</tbody></table></div></section>; }

function Procurement({ purchases, suppliers, items, alerts, replenishment, threshold, canManage, setThreshold, add, agentInstruction, setAgentInstruction, agentBusy, agentResult, agentError, runAgent, approveProposal, showIngest, ingestBusy, ingestResult, runSupplierIngest }: { purchases: Purchase[]; suppliers: Supplier[]; items: Item[]; alerts: Purchase[]; replenishment: Replenishment[]; threshold: number; canManage: boolean; setThreshold: (value: number) => void; add: () => void; agentInstruction: string; setAgentInstruction: (value: string) => void; agentBusy: boolean; agentResult: AgentRunResponse | null; agentError: string; runAgent: () => void; approveProposal: (proposal: AgentProposal) => void; showIngest: boolean; ingestBusy: boolean; ingestResult: SupplierPriceIngestResponse | null; runSupplierIngest: () => void }) { return <><div className="procurement-top"><section className="panel price-policy"><div><p className="eyebrow">Price monitoring</p><h3>Alert when a purchase price rises by</h3><p className="small-copy">Compare the newest price with the prior purchase of the same item.</p></div>{canManage ? <label className="threshold"><input type="number" min="1" defaultValue={threshold} key={threshold} onBlur={event => setThreshold(Number(event.currentTarget.value) || 1)} />%</label> : <strong>{threshold}%</strong>}</section><section className="panel supplier-summary"><PanelHeading title="Supplier coverage" note="Preferred and backup options" />{suppliers.map(supplier => <div key={supplier.id} className="supplier-row"><div><b>{supplier.name}</b><small>{supplier.leadDays}-day lead time · {supplier.rating}/5 rating</small></div><Status tone={supplier.status === 'preferred' ? 'good' : supplier.status === 'paused' ? 'danger' : 'neutral'}>{supplier.status}</Status></div>)}</section></div>{showIngest && <section className="panel ingest-panel"><div className="panel-heading"><div><h3>Supplier price submissions</h3><p>Drain the webhook inbox suppliers push signed price quotes into via API Gateway.</p></div><button onClick={runSupplierIngest} disabled={ingestBusy}>{ingestBusy ? 'Checking…' : 'Check now'}</button></div>{ingestResult && <div className="ingest-summary"><p>{ingestResult.processed} submission{ingestResult.processed === 1 ? '' : 's'} processed.</p><div className="ingest-chips">{ingestResult.results.map((result, index) => <span key={index} className={`ingest-chip ${result.status}`}>{result.sku || 'unknown'}: {result.status.replaceAll('_', ' ')}{result.price_change_percent !== undefined ? ` (${result.price_change_percent > 0 ? '+' : ''}${result.price_change_percent}%)` : ''}</span>)}{!ingestResult.results.length && <span className="ingest-chip recorded">Inbox is empty</span>}</div></div>}</section>}<section className="panel agent-panel"><PanelHeading title="Procurement agent" note="Ask what to reorder. It can only propose a purchase, never place one." />{agentError && <p className="agent-error">{agentError}</p>}<div className="agent-ask"><input value={agentInstruction} onChange={event => setAgentInstruction(event.target.value)} placeholder="What should we reorder this week?" disabled={agentBusy} /><button className="primary" onClick={runAgent} disabled={agentBusy}><Bot size={16} />{agentBusy ? 'Thinking…' : 'Ask the agent'}</button></div>{agentResult && <div className="agent-result"><p className="agent-summary">{agentResult.summary}</p>{agentResult.steps.length > 0 && <div className="agent-steps">{agentResult.steps.map((step, index) => <span key={index} className={`agent-step ${step.ok ? 'ok' : 'failed'}`}>{step.tool}</span>)}</div>}{agentResult.proposals.map(proposal => <div className="proposal-row" key={`${proposal.sku}-${proposal.supplier_id}`}><div><b>{proposal.item_name}</b><small>{proposal.reason}</small></div><div><strong>Order {proposal.quantity} {proposal.unit}</strong><small>{proposal.supplier_name}{proposal.differs_from_engine ? ` · engine suggested ${proposal.engine_suggested_quantity}` : ''}</small></div><button className="secondary" onClick={() => approveProposal(proposal)}>Review &amp; approve</button></div>)}{!agentResult.proposals.length && <Empty label="Nothing needs ordering right now." />}</div>}</section>{replenishment.length > 0 && <section className="panel recommendation-panel"><PanelHeading title="Recommended purchase plan" note="Ranked by latest price, lead time, supplier rating, and preferred status" />{replenishment.map(recommendation => <div className="recommendation-row" key={recommendation.itemId}><div><b>{recommendation.itemName}</b><small>{recommendation.quantityOnHand} {recommendation.unit} on hand · {recommendation.daysOfCover === null ? 'low stock' : `${recommendation.daysOfCover} days of cover`}</small></div><div><strong>Order {recommendation.suggestedQuantity} {recommendation.unit}</strong><small>{recommendation.recommendedSupplier ? `Recommended: ${recommendation.recommendedSupplier.supplierName} · ${money(recommendation.recommendedSupplier.unitCost, recommendation.recommendedSupplier.currency)} · ${recommendation.recommendedSupplier.leadDays}-day lead time` : 'No supplier price history yet'}</small></div></div>)}</section>}<section className="panel"><div className="table-tools"><div><b>Purchases and price history</b><small className="block-note">Each receipt updates price history and may produce an alert.</small></div><button className="primary" onClick={add}><Plus size={17} />Receive purchase</button></div><div className="table-wrap"><table><thead><tr><th>Purchase date</th><th>Item / supplier</th><th>Quantity</th><th>Unit cost</th><th>Change</th><th>Attachment</th></tr></thead><tbody>{purchases.map(purchase => { const item = items.find(entry => entry.id === purchase.itemId); const supplier = suppliers.find(entry => entry.id === purchase.supplierId); return <tr key={purchase.id}><td>{date(purchase.at)}</td><td><b>{item?.name}</b><small>{supplier?.name} · {purchase.invoice}</small></td><td>{purchase.qty} {item?.unit}</td><td><b>{money(purchase.unitCost, purchase.currency)}</b></td><td><span className={purchase.priceChange >= threshold ? 'price-up' : purchase.priceChange < 0 ? 'price-down' : 'price-flat'}>{purchase.priceChange > 0 ? <TrendingUp size={14} /> : purchase.priceChange < 0 ? <TrendingDown size={14} /> : null}{purchase.priceChange === 0 ? 'First price' : `${purchase.priceChange > 0 ? '+' : ''}${purchase.priceChange}%`}</span></td><td><ReceiptAttachment name={purchase.receipt} url={purchase.receiptUrl} /></td></tr>; })}</tbody></table></div></section>{alerts.length > 0 && <section className="panel alert-panel"><PanelHeading title="Supplier review recommended" note="These purchases exceeded the configured price-increase threshold." />{alerts.map(alert => { const item = items.find(entry => entry.id === alert.itemId); const alternatives = purchases.filter(entry => entry.itemId === alert.itemId && entry.supplierId !== alert.supplierId).slice(0, 2); return <div key={alert.id} className="alert-row"><TriangleAlert size={19} /><div><b>{item?.name} increased {alert.priceChange}%</b><small>Current purchase: {money(alert.unitCost)}. Alternative recent prices: {alternatives.map(entry => money(entry.unitCost)).join(', ') || 'No alternate supplier data yet'}.</small></div></div>; })}</section>}</>;
}

function Expenses({ expenses, items, canApprove, canPay, add, update }: { expenses: Expense[]; items: Item[]; canApprove: boolean; canPay: boolean; add: () => void; update: (expense: Expense, status: ExpenseStatus) => void }) { return <section className="panel"><div className="table-tools"><div><b>Reimbursements</b><small className="block-note">Receipts, approval history, and payment state.</small></div><button className="primary" onClick={add}><Plus size={17} />Submit expense</button></div><div className="table-wrap"><table><thead><tr><th>Submitted</th><th>Employee / item</th><th>Supplier / purpose</th><th>Receipt</th><th>Amount</th><th>Status</th><th></th></tr></thead><tbody>{expenses.map(expense => { const item = items.find(entry => entry.id === expense.itemId); return <tr key={expense.id}><td>{date(expense.at)}</td><td><b>{expense.submitter}</b><small>{item?.name} · {expense.qty} {item?.unit}</small></td><td><b>{expense.supplier}</b><small>{expense.purpose}</small></td><td><ReceiptAttachment name={expense.receipt} url={expense.receiptUrl} /></td><td><b>{money(expense.amount, expense.currency)}</b></td><td><Status tone={expense.status === 'paid' || expense.status === 'approved' ? 'good' : expense.status === 'rejected' ? 'danger' : 'warn'}>{expense.status}</Status>{expense.reviewer && <small>{expense.reviewer}</small>}</td><td><div className="row-actions">{expense.status === 'submitted' && canApprove && <><IconButton label="Approve reimbursement" onClick={() => update(expense, 'approved')}><Check size={16} /></IconButton><IconButton label="Reject reimbursement" onClick={() => update(expense, 'rejected')}><XCircle size={16} /></IconButton></>}{expense.status === 'approved' && canPay && <button className="pay-button" onClick={() => update(expense, 'paid')}>Mark paid</button>}</div></td></tr>; })}{!expenses.length && <tr><td colSpan={7}><Empty label="No reimbursement submissions in your visible scope." /></td></tr>}</tbody></table></div></section>; }

function AuditLog({ audits, eventFilters, setEventFilters, events, eventsBusy, search }: { audits: Audit[]; eventFilters: { action: string; minQuantity: string; minPriceChangePercent: string }; setEventFilters: (value: { action: string; minQuantity: string; minPriceChangePercent: string }) => void; events: AuditEventResponse[] | null; eventsBusy: boolean; search: () => void }) {
  return <><section className="panel audit-search-panel"><PanelHeading title="Search structured audit events" note="Query the MongoDB documents by fields a specific action actually carries — not just who and when." /><div className="audit-filters"><input placeholder="Action contains… (e.g. issued_stock)" value={eventFilters.action} onChange={event => setEventFilters({ ...eventFilters, action: event.target.value })} /><input placeholder="Min quantity" type="number" value={eventFilters.minQuantity} onChange={event => setEventFilters({ ...eventFilters, minQuantity: event.target.value })} /><input placeholder="Min price change %" type="number" value={eventFilters.minPriceChangePercent} onChange={event => setEventFilters({ ...eventFilters, minPriceChangePercent: event.target.value })} /><button className="secondary" onClick={search} disabled={eventsBusy}><Search size={15} />{eventsBusy ? 'Searching…' : 'Search'}</button></div>{events !== null && <div className="audit-events-list">{events.map(event => <div key={event.id} className="audit-event-row"><div><b>{event.action}</b><small>{event.actor.name} · {roleLabel[event.actor.role]} · {date(event.created_at)}</small></div><p className="event-detail">{event.detail}</p><div className="event-payload">{Object.entries(event.payload).filter(([key]) => !key.endsWith('_id')).map(([key, value]) => <span key={key} className="payload-chip">{key.replaceAll('_', ' ')}: {String(value)}</span>)}</div></div>)}{!events.length && <Empty label="No structured events match this search." />}</div>}</section><section className="panel"><PanelHeading title="Administrative audit log" note="Immutable history of the actions performed in this system." /><div className="table-wrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Role</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead><tbody>{audits.map(entry => <tr key={entry.id}><td>{date(entry.at)}</td><td><b>{entry.actor}</b></td><td><Status tone={entry.role === 'admin' ? 'good' : entry.role === 'supervisor' ? 'neutral' : 'warn'}>{roleLabel[entry.role]}</Status></td><td>{entry.action}</td><td><b>{entry.target}</b></td><td>{entry.detail}</td></tr>)}</tbody></table></div></section></>;
}
function AppearanceSettings({ preference, companyDefault, canManage, updatePreference, updateCompanyDefault }: { preference: AppearancePreference; companyDefault: Appearance; canManage: boolean; updatePreference: (value: AppearancePreference | ((value: AppearancePreference) => AppearancePreference)) => void; updateCompanyDefault: (value: Appearance) => void }) {
  const { t } = useI18n();
  const updatePersonal = (next: Partial<Appearance>) => updatePreference(current => ({ ...current, ...next, followCompanyDefault: false }));
  return <div className="settings-grid"><section className="panel appearance-panel"><PanelHeading title={t('myAppearance', 'My appearance')} note={t('appearanceNote', 'Choose how this workspace looks on this device.')} /><label className="toggle-row"><span><b>{t('followCompany', 'Follow company default')}</b><small>{t('followCompanyNote', 'Use the appearance chosen by your administrator.')}</small></span><input type="checkbox" checked={preference.followCompanyDefault} onChange={event => updatePreference(current => ({ ...current, followCompanyDefault: event.target.checked }))} /></label>{!preference.followCompanyDefault && <AppearanceControls value={preference} onChange={updatePersonal} />}</section>{canManage && <section className="panel appearance-panel"><PanelHeading title={t('companyAppearance', 'Company default')} note={t('companyAppearanceNote', 'New users and people following the default will see these settings.')} /><AppearanceControls value={companyDefault} onChange={next => updateCompanyDefault({ ...companyDefault, ...next })} /></section>}</div>;
}
function AppearanceControls({ value, onChange }: { value: Appearance; onChange: (value: Partial<Appearance>) => void }) {
  const { t } = useI18n();
  const modes: { value: ThemeMode; label: string }[] = [{ value: 'light', label: t('light', 'Light') }, { value: 'dark', label: t('dark', 'Dark') }, { value: 'system', label: t('system', 'System') }];
  const accents: { value: Accent; label: string }[] = [{ value: 'green', label: t('green', 'Green') }, { value: 'blue', label: t('blue', 'Blue') }, { value: 'orange', label: t('orange', 'Orange') }];
  return <div className="appearance-controls"><div><b>{t('theme', 'Theme')}</b><div className="segmented">{modes.map(option => <button key={option.value} type="button" className={value.mode === option.value ? 'selected' : ''} onClick={() => onChange({ mode: option.value })} aria-pressed={value.mode === option.value}>{option.label}</button>)}</div></div><div><b>{t('accentColor', 'Accent color')}</b><div className="swatches">{accents.map(option => <button key={option.value} type="button" className={value.accent === option.value ? `selected ${option.value}` : option.value} onClick={() => onChange({ accent: option.value })} aria-label={option.label} title={option.label} aria-pressed={value.accent === option.value}><span /></button>)}</div></div><div><b>{t('density', 'Information density')}</b><div className="segmented">{(['comfortable', 'compact'] as Density[]).map(option => <button key={option} type="button" className={value.density === option ? 'selected' : ''} onClick={() => onChange({ density: option })} aria-pressed={value.density === option}>{t(option, option === 'comfortable' ? 'Comfortable' : 'Compact')}</button>)}</div></div></div>;
}
function ReceiptAttachment({ name, url }: { name: string; url?: string }) {
  const content = <><FileText size={14} />{name}</>;
  if (url) return <a className="file-name" href={url} target="_blank" rel="noreferrer">{content}</a>;
  if (name === 'No attachment') return <span className="file-name">{content}</span>;
  const open = async () => {
    const tab = window.open('', '_blank');
    try { const response = await api<{ download_url: string }>(`/api/attachments/download?key=${encodeURIComponent(name)}`); if (tab) tab.location.href = response.download_url; else window.location.href = response.download_url; }
    catch { tab?.close(); }
  };
  return <button type="button" className="file-name" onClick={open}>{content}</button>;
}
function PanelHeading({ title, note, action, onClick }: { title: string; note: string; action?: string; onClick?: () => void }) { return <div className="panel-heading"><div><h3>{title}</h3><p>{note}</p></div>{action && <button onClick={onClick}>{action}</button>}</div>; }
function MovementRow({ movement, item }: { movement: Movement; item?: Item }) { const inbound = movement.kind === 'inbound'; return <div className="move-row"><span className={`move-icon ${inbound ? 'in' : 'out'}`}>{inbound ? <ArrowDownToLine size={16} /> : <ArrowUpFromLine size={16} />}</span><div><b>{item?.name || 'Deleted item'}</b><small>{movement.recipient} · {date(movement.at)}</small></div><strong className={inbound ? 'amount-in' : 'amount-out'}>{inbound ? '+' : '-'}{movement.qty} {item?.unit}</strong></div>; }
function Status({ tone, children }: { tone: 'good' | 'warn' | 'danger' | 'neutral'; children: ReactNode }) { return <i className={`status ${tone}`}>{children}</i>; }
function Empty({ label }: { label: string }) { return <div className="empty">{label}</div>; }
function IconButton({ label, children, onClick }: { label: string; children: ReactNode; onClick?: () => void }) { return <button type="button" aria-label={label} title={label} onClick={onClick} className="icon-button">{children}</button>; }
function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
function Modal({ title, close, children }: { title: string; close: () => void; children: ReactNode }) { return <div className="modal-backdrop"><section className="modal" role="dialog" aria-label={title} aria-modal="true"><div className="modal-head"><h3>{title}</h3><IconButton label="Close dialog" onClick={close}><X size={18} /></IconButton></div>{children}</section></div>; }
function BarcodeScanner({ onDetected }: { onDetected: (value: string) => void }) {
  const scannerId = `barcode-reader-${useId().replaceAll(':', '')}`;
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    // Take the scanner's real type from the library rather than describing it
    // by hand: a hand-written shape drifts, and a loose one hides mistakes.
    // This is a type-only import, so it adds nothing to the bundle.
    type Scanner = InstanceType<typeof import('html5-qrcode').Html5Qrcode>;
    let scanner: Scanner | undefined;
    const start = async () => {
      try {
        const { Html5Qrcode } = await import('html5-qrcode');
        if (!active) return;
        const instance = new Html5Qrcode(scannerId);
        scanner = instance;
        await instance.start({ facingMode: 'environment' }, { fps: 10, qrbox: { width: 220, height: 140 } }, (decodedText: string) => { instance.pause(true); onDetected(decodedText); }, () => undefined);
      } catch {
        if (active) setError('Camera scanning is unavailable. Check camera permission or enter the SKU manually.');
      }
    };
    void start();
    return () => { active = false; void scanner?.stop().catch(() => undefined); };
  }, [onDetected, scannerId]);
  return <div className="scanner-box"><div id={scannerId} className="scanner-video" />{error && <small>{error}</small>}</div>;
}

function MovementDialog({ title, kind, items, close, submit }: { title: string; kind: Kind; items: Item[]; close: () => void; submit: (event: FormEvent<HTMLFormElement>, kind: Kind) => void }) {
  const [itemId, setItemId] = useState('');
  const [scanOpen, setScanOpen] = useState(false);
  const [scanError, setScanError] = useState('');
  const detected = (value: string) => {
    const item = items.find(entry => entry.sku.toLowerCase() === value.trim().toLowerCase());
    if (!item) { setScanError(`No item matches scanned code “${value}”.`); return; }
    setItemId(item.id); setScanOpen(false); setScanError('');
  };
  return <Modal title={title} close={close}><form onSubmit={event => submit(event, kind)}><Field label="Item"><select name="item" required value={itemId} onChange={event => setItemId(event.currentTarget.value)}><option value="" disabled>Select an item</option>{items.map(item => <option key={item.id} value={item.id}>{item.name} ({item.qty} {item.unit} available)</option>)}</select></Field>{kind === 'outbound' && <><button type="button" className="scan-trigger" onClick={() => { setScanOpen(value => !value); setScanError(''); }}><ScanBarcode size={17} />{scanOpen ? 'Close camera scanner' : 'Scan item barcode'}</button>{scanOpen && <BarcodeScanner onDetected={detected} />}{scanError && <p className="scan-error">{scanError}</p>}</>}<div className="form-grid"><Field label="Quantity"><input name="qty" type="number" required min="1" placeholder="0" /></Field><Field label={kind === 'inbound' ? 'Supplier / source' : 'Issued to'}><input name="recipient" required placeholder={kind === 'inbound' ? 'Supplier name' : 'Team or employee'} /></Field></div><Field label="Reference note"><input name="note" placeholder="Reason, PO, or project" /></Field><div className="modal-actions"><button type="button" className="secondary" onClick={close}>Cancel</button><button className="primary">Record {kind === 'inbound' ? 'receipt' : 'issue'}</button></div></form></Modal>;
}
function PurchaseDialog({ items, suppliers, recommendations, seed, close, submit }: { items: Item[]; suppliers: Supplier[]; recommendations: Replenishment[]; seed?: { itemId: string; supplierId: string; quantity: number } | null; close: () => void; submit: (event: FormEvent<HTMLFormElement>) => void }) {
  const [itemId, setItemId] = useState(seed?.itemId || '');
  const [supplierId, setSupplierId] = useState(seed?.supplierId || '');
  const recommendation = recommendations.find(entry => entry.itemId === itemId);
  // Priced from whoever is actually selected, not from whoever the engine ranked
  // first. An agent may propose a lower-ranked supplier and a reviewer may pick
  // one by hand; both used to leave the top-ranked supplier's price in the field,
  // recording a cost the chosen supplier never quoted.
  const selectedOption = recommendation?.alternatives.find(option => option.supplierId === supplierId) || recommendation?.recommendedSupplier;
  const selectItem = (nextItemId: string) => { setItemId(nextItemId); setSupplierId(recommendations.find(entry => entry.itemId === nextItemId)?.recommendedSupplier?.supplierId || ''); };
  return <Modal title="Receive purchase" close={close}><form onSubmit={submit}>{seed && <p className="seed-note"><Bot size={14} />Pre-filled from an agent proposal — review before submitting.</p>}<Field label="Item"><select name="item" required value={itemId} onChange={event => selectItem(event.currentTarget.value)}><option value="" disabled>Select an item</option>{items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>{recommendation && <div className="supplier-advice"><b>Purchase guidance</b><span>Order {recommendation.suggestedQuantity} {recommendation.unit}. {recommendation.daysOfCover === null ? 'Inventory is below its minimum level.' : `${recommendation.daysOfCover} days of cover remaining.`}</span>{recommendation.alternatives.map(option => <small key={option.supplierId} className={option.supplierId === recommendation.recommendedSupplier?.supplierId ? 'recommended' : ''}>{option.supplierName}: {money(option.unitCost, option.currency)} · {option.leadDays}-day lead time · score {option.score}</small>)}</div>}<div className="form-grid"><Field label="Supplier"><select name="supplier" required value={supplierId} onChange={event => setSupplierId(event.currentTarget.value)}><option value="" disabled>Select supplier</option>{suppliers.filter(supplier => supplier.status !== 'paused').map(supplier => <option key={supplier.id} value={supplier.id}>{supplier.name}</option>)}</select></Field><Field label="Purchase quantity"><input key={`${itemId}-quantity`} name="qty" required type="number" min="1" defaultValue={seed?.quantity || recommendation?.suggestedQuantity || ''} placeholder="0" /></Field><Field label="Unit cost (USD)"><input key={`${itemId}-${supplierId}-cost`} name="unitCost" required type="number" min="0.01" step="0.01" defaultValue={selectedOption?.unitCost || ''} placeholder="0.00" /></Field><Field label="Invoice / PO number"><input name="invoice" placeholder="e.g. INV-2201" /></Field></div><Field label="Receipt or invoice attachment"><input name="receipt" type="file" accept="image/*,.pdf" /></Field><div className="modal-actions"><button type="button" className="secondary" onClick={close}>Cancel</button><button className="primary">Receive and record cost</button></div></form></Modal>;
}
function ExpenseDialog({ items, close, submit }: { items: Item[]; close: () => void; submit: (event: FormEvent<HTMLFormElement>) => void }) { return <Modal title="Submit reimbursement" close={close}><form onSubmit={submit}><Field label="Item purchased"><select name="item" required defaultValue=""><option value="" disabled>Select item</option>{items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><div className="form-grid"><Field label="Store / supplier"><input name="supplier" required placeholder="Merchant name" /></Field><Field label="Quantity"><input name="qty" required type="number" min="1" placeholder="0" /></Field><Field label="Total amount (USD)"><input name="amount" required type="number" min="0.01" step="0.01" placeholder="0.00" /></Field><Field label="Receipt attachment"><input name="receipt" type="file" required accept="image/*,.pdf" /></Field></div><Field label="Business purpose"><input name="purpose" required placeholder="Why was this purchase needed?" /></Field><div className="modal-actions"><button type="button" className="secondary" onClick={close}>Cancel</button><button className="primary">Submit for approval</button></div></form></Modal>; }
function ItemDialog({ close, submit }: { close: () => void; submit: (event: FormEvent<HTMLFormElement>) => void }) { return <Modal title="Add inventory item" close={close}><form onSubmit={submit}><div className="form-grid"><Field label="Item name"><input name="name" required placeholder="e.g. Label printer" /></Field><Field label="SKU"><input name="sku" placeholder="e.g. EQ-PRN-001" /></Field><Field label="Category"><input name="category" placeholder="e.g. Electronics" /></Field><Field label="Storage location"><input name="location" placeholder="e.g. A-02-03" /></Field><Field label="Starting quantity"><input name="qty" type="number" min="0" placeholder="0" /></Field><Field label="Minimum level"><input name="min" type="number" min="0" placeholder="0" /></Field></div><Field label="Unit"><input name="unit" defaultValue="pcs" /></Field><div className="modal-actions"><button type="button" className="secondary" onClick={close}>Cancel</button><button className="primary">Add item</button></div></form></Modal>; }
function LocalizedLogin({ onLogin, demoAccounts: localAccounts }: { onLogin: (email: string, password: string) => Promise<void>; demoAccounts?: User[] }) {
  const { t } = useI18n();
  type AuthMode = 'sign-in' | 'sign-up' | 'confirm' | 'reset' | 'new-password';
  const [mode, setMode] = useState<AuthMode>('sign-in');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get('email')).trim().toLowerCase();
    const password = String(form.get('password'));
    const code = String(form.get('code')).trim();
    const name = String(form.get('name')).trim();
    setSubmitting(true); setError('');
    try {
      if (mode === 'sign-in') await onLogin(email, password);
      if (mode === 'sign-up') { await registerEmployee(email, password, name); setMode('confirm'); setMessage(t('verificationSent', 'A verification code was sent to your email.')); }
      if (mode === 'confirm') { await confirmEmployeeRegistration(email, code); setMode('sign-in'); setMessage(t('emailConfirmed', 'Email confirmed. You can now sign in.')); }
      if (mode === 'reset') { await beginPasswordReset(email); setMode('new-password'); setMessage(t('resetSent', 'A password reset code was sent to your email.')); }
      if (mode === 'new-password') { await finishPasswordReset(email, code, password); setMode('sign-in'); setMessage(t('passwordUpdated', 'Password updated. You can now sign in.')); }
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : t('incorrectLogin', 'Incorrect email or password.')); }
    finally { setSubmitting(false); }
  };
  const heading = mode === 'sign-up' ? t('createEmployeeAccount', 'Create an employee account') : mode === 'confirm' ? t('confirmEmail', 'Confirm your email') : mode === 'reset' ? t('resetPassword', 'Reset your password') : mode === 'new-password' ? t('newPassword', 'Choose a new password') : t('signInTitle', 'Sign in to your workspace');
  const note = mode === 'sign-up' ? t('registrationNote', 'New registrations receive employee access. Administrators manage supervisor and administrator roles.') : mode === 'confirm' ? t('confirmationNote', 'Enter the verification code sent to your email address.') : mode === 'reset' || mode === 'new-password' ? t('recoveryNote', 'Use your work email to recover secure access.') : t('loginNote', 'Use the account assigned by your warehouse administrator.');
  const submitLabel = mode === 'sign-up' ? t('createAccount', 'Create account') : mode === 'confirm' ? t('confirmEmailButton', 'Confirm email') : mode === 'reset' ? t('sendResetCode', 'Send reset code') : mode === 'new-password' ? t('setNewPassword', 'Set new password') : t('signIn', 'Sign in');
  const switchMode = (next: AuthMode) => { setMode(next); setError(''); setMessage(''); };
  return <main className="login-page"><section className="login-card"><div className="login-top"><div className="login-brand"><span className="brand-mark"><Package size={21} /></span><span><b>Stockroom</b><small>Warehouse operations</small></span></div><LanguageSelect /></div><div className="login-copy"><p><ShieldCheck size={15} /> {t('secureAccess', 'Secure access')}</p><h1>{heading}</h1><span>{note}</span></div><form onSubmit={submit}>{mode === 'sign-up' && <Field label={t('fullName', 'Full name')}><input name="name" required autoComplete="name" placeholder={t('fullName', 'Full name')} /></Field>}<Field label={t('email', 'Email')}><input name="email" required type="email" autoComplete="username" placeholder="name@company.com" /></Field>{(mode === 'confirm' || mode === 'new-password') && <Field label={t('verificationCode', 'Verification code')}><input name="code" required inputMode="numeric" autoComplete="one-time-code" placeholder="123456" /></Field>}{(mode === 'sign-in' || mode === 'sign-up' || mode === 'new-password') && <Field label={mode === 'new-password' ? t('newPassword', 'New password') : t('password', 'Password')}><input name="password" required type="password" minLength={12} autoComplete={mode === 'sign-in' ? 'current-password' : 'new-password'} placeholder={t('password', 'Password')} /></Field>}{error && <p className="login-error">{error}</p>}{message && <p className="login-message">{message}</p>}<button className="primary login-submit" disabled={submitting}>{submitting ? t('pleaseWait', 'Please wait...') : submitLabel}</button></form><div className="auth-links">{mode === 'sign-in' && <>{!localAccounts && <><button type="button" onClick={() => switchMode('sign-up')}>{t('createEmployeeAccount', 'Create employee account')}</button><button type="button" onClick={() => switchMode('reset')}>{t('forgotPassword', 'Forgot password?')}</button></>}</>}{mode === 'sign-up' && <button type="button" onClick={() => switchMode('sign-in')}>{t('alreadyHaveAccount', 'Already have an account? Sign in')}</button>}{mode === 'confirm' && <button type="button" onClick={() => switchMode('sign-in')}>{t('backToSignIn', 'Back to sign in')}</button>}{(mode === 'reset' || mode === 'new-password') && <button type="button" onClick={() => switchMode('sign-in')}>{t('backToSignIn', 'Back to sign in')}</button>}</div>{localAccounts && mode === 'sign-in' && <section className="demo-accounts"><p>Local test accounts</p>{localAccounts.map(account => <article key={account.email}><span>{roleLabel[account.role]}</span><b>{account.email}</b><small>Password: {account.password}</small></article>)}</section>}</section></main>;
}
