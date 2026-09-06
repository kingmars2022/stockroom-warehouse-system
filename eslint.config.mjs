import { defineConfig, globalIgnores } from 'eslint/config';
import nextVitals from 'eslint-config-next/core-web-vitals';
import nextTs from 'eslint-config-next/typescript';

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // `backend/` is the Python service; its virtualenv ships vendored JS that is
  // not ours to lint.
  globalIgnores([
    '.next/**',
    'out/**',
    'build/**',
    'dist/**',
    'node_modules/**',
    'backend/**',
    'next-env.d.ts',
  ]),
]);

export default eslintConfig;
