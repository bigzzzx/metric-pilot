// Separate ports keep browser acceptance away from a user's normal application.
import { mergeConfig } from 'vite';
import base from '../vite.config.js';

export default mergeConfig(base, {
  server: { port: 5374, proxy: { '/api': 'http://127.0.0.1:3301' } },
});
