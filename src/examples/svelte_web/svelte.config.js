// Configures SvelteKit build options and adapter-node settings to emit a standalone SSR web server.

import adapter from "@sveltejs/adapter-node";

const config = {
  kit: {
    adapter: adapter(),
    files: {
      appTemplate: "app.html",
      hooks: {
        client: "hooks.client.ts",
        server: "hooks.server.ts",
      },
      routes: "routes",
    },
  },
};

export default config;
