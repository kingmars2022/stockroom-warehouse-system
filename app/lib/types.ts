/**
 * Response shapes returned by the Stockroom API.
 *
 * These mirror the Pydantic models in `backend/app/schemas.py` and keep their
 * snake_case field names, so the boundary where the API payload is mapped into
 * the UI's camelCase view models stays explicit and type-checked.
 */

export type Role = 'admin' | 'supervisor' | 'employee';
export type MovementKind = 'inbound' | 'outbound';
export type ExpenseStatus = 'submitted' | 'approved' | 'rejected' | 'paid';
export type SupplierStatus = 'preferred' | 'backup' | 'paused';

export interface MeResponse {
  id: string;
  email: string;
  name: string;
  role: Role;
}

export interface ItemResponse {
  id: string;
  sku: string;
  name: string;
  category: string;
  location: string;
  unit: string;
  quantity_on_hand: number;
  minimum_quantity: number;
  created_at: string;
  updated_at: string;
}

export interface SupplierResponse {
  id: string;
  name: string;
  contact: string;
  lead_days: number;
  rating: number;
  status: SupplierStatus;
}

export interface MovementResponse {
  id: string;
  item_id: string;
  kind: MovementKind;
  quantity: number;
  actor_id: string;
  actor_name: string;
  recipient: string;
  note: string;
  created_at: string;
}

export interface PurchaseResponse {
  id: string;
  item_id: string;
  supplier_id: string;
  received_by_id: string;
  quantity: number;
  unit_cost: number;
  currency: string;
  invoice_number: string;
  receipt_key: string | null;
  price_change_percent: number;
  created_at: string;
}

export interface ExpenseResponse {
  id: string;
  submitter_id: string;
  submitter_name: string;
  item_id: string | null;
  supplier: string;
  quantity: number;
  amount: number;
  currency: string;
  purpose: string;
  receipt_key: string | null;
  status: ExpenseStatus;
  reviewer_id: string | null;
  reviewer_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditResponse {
  id: string;
  actor_id: string;
  actor_name: string;
  actor_role: Role;
  action: string;
  target_type: string;
  target_id: string;
  detail: string;
  created_at: string;
}

export interface SupplierRecommendation {
  supplier_id: string;
  supplier_name: string;
  unit_cost: number;
  currency: string;
  lead_days: number;
  rating: number;
  score: number;
}

export interface ReplenishmentResponse {
  item_id: string;
  item_name: string;
  sku: string;
  unit: string;
  quantity_on_hand: number;
  daily_usage: number;
  days_of_cover: number | null;
  suggested_quantity: number;
  recommended_supplier: SupplierRecommendation | null;
  alternatives: SupplierRecommendation[];
}

export interface PricePolicyResponse {
  threshold_percent: number;
}
