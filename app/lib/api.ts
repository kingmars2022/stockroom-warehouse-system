'use client';

import { idToken } from './auth';

const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await idToken();
  const response = await fetch(`${apiUrl}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...init.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string };
    throw new ApiError(response.status, body.detail || 'The server could not complete this request.');
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

// The API answers 409 for two unrelated situations: a receipt that has not
// been validated yet, and a genuine business conflict such as insufficient
// stock. Only the first is worth retrying, and the message is the only thing
// that separates them — see assert_receipt_validated in services.py.
const RECEIPT_PENDING = /still being validated/i;

/**
 * Upload a receipt at most once, then retry only the record that references it.
 *
 * Validation is asynchronous, so the first attempt to attach a freshly
 * uploaded receipt can legitimately lose the race and come back 409. Re-running
 * the whole submission would upload a *new* file with a new key, restart the
 * same race, and leave the previous object orphaned in the bucket — so the key
 * is captured once and reused for every attempt.
 */
export async function withReceipt<T>(
  file: File | undefined,
  send: (receiptKey?: string) => Promise<T>,
  attempts = 4,
): Promise<T> {
  const receiptKey = file?.name ? await uploadReceipt(file) : undefined;
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await send(receiptKey);
    } catch (error) {
      const pending = error instanceof ApiError && error.status === 409 && RECEIPT_PENDING.test(error.message);
      if (!pending || attempt >= attempts) throw error;
      await new Promise(resolve => setTimeout(resolve, 600 * (attempt + 1)));
    }
  }
}

export async function uploadReceipt(file: File): Promise<string> {
  const intent = await api<{ key: string; upload_url: string }>('/api/attachments/presign', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
  });
  const response = await fetch(intent.upload_url, { method: 'PUT', headers: { 'Content-Type': file.type }, body: file });
  if (!response.ok) throw new Error('Receipt upload failed.');
  return intent.key;
}
