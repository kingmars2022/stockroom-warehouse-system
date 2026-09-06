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

export async function uploadReceipt(file: File): Promise<string> {
  const intent = await api<{ key: string; upload_url: string }>('/api/attachments/presign', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, content_type: file.type, size_bytes: file.size }),
  });
  const response = await fetch(intent.upload_url, { method: 'PUT', headers: { 'Content-Type': file.type }, body: file });
  if (!response.ok) throw new Error('Receipt upload failed.');
  return intent.key;
}
