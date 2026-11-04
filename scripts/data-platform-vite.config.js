import { defineConfig, mergeConfig } from 'vite';
import base from '../vite.config.js';

export default defineConfig(env => mergeConfig(base(env), {
  server: { port: 5375, proxy: { '/api': 'http://127.0.0.1:3302' } },
}));
