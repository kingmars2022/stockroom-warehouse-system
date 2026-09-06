'use client';

import { Amplify } from 'aws-amplify';
import {
  confirmResetPassword,
  confirmSignUp,
  fetchAuthSession,
  resetPassword,
  signIn,
  signOut,
  signUp,
} from 'aws-amplify/auth';

let configured = false;

function configureAuth() {
  if (configured) return;
  const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID;
  const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_APP_CLIENT_ID;
  if (!userPoolId || !userPoolClientId) throw new Error('Secure sign-in is still being configured. Ask an administrator to complete the Cognito setup.');
  Amplify.configure({ Auth: { Cognito: { userPoolId, userPoolClientId } } }, { ssr: true });
  configured = true;
}

export async function authenticate(email: string, password: string) {
  configureAuth();
  const result = await signIn({ username: email, password });
  if (!result.isSignedIn) throw new Error('Additional Cognito sign-in verification is required.');
}

export async function registerEmployee(email: string, password: string, name: string) {
  configureAuth();
  await signUp({
    username: email,
    password,
    options: { userAttributes: { email, name } },
  });
}

export async function confirmEmployeeRegistration(email: string, confirmationCode: string) {
  configureAuth();
  await confirmSignUp({ username: email, confirmationCode });
}

export async function beginPasswordReset(email: string) {
  configureAuth();
  await resetPassword({ username: email });
}

export async function finishPasswordReset(email: string, confirmationCode: string, newPassword: string) {
  configureAuth();
  await confirmResetPassword({ username: email, confirmationCode, newPassword });
}

export async function idToken() {
  configureAuth();
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken?.toString();
  if (!token) throw new Error('Your session has expired. Please sign in again.');
  return token;
}

export async function logout() {
  configureAuth();
  await signOut();
}
