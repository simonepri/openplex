<!-- Renders the root web page showing server-rendered pod identity, request timestamps, and PostgreSQL visit counts. -->

<script lang="ts">
  import type { PageData } from "./$types";

  const { data }: { data: PageData } = $props();
</script>

<main>
  <h1>Svelte example</h1>
  <p>
    Server-rendered by pod <code>{data.server}</code>
    at <code>{data.renderedAt}</code>, deployed by Argo CD from a
    Bazel-built image.
  </p>
  <p>
    Page views: <strong>{data.views}</strong> (storage: <code>{data.storage}</code>)
  </p>
</main>

<style>
  main {
    font-family: system-ui, sans-serif;
    max-width: 40rem;
    margin: 4rem auto;
    text-align: center;
  }
</style>
