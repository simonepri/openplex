// Configures Vite bundler plugins and compilation settings for the SvelteKit example application.

import { defineConfig } from "vite";
import { sveltekit } from "@sveltejs/kit/vite";

export default defineConfig({
  plugins: [sveltekit()],
});
