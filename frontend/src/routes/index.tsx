import { createFileRoute } from "@tanstack/react-router";

export const Route = createFileRoute("/")({
  component: HubPage,
});

function HubPage() {
  return (
    <main className="mx-auto max-w-4xl p-8">
      <h1 className="text-2xl font-semibold">Claude Developer Hub</h1>
      <p className="mt-2 text-zinc-400">
        Hub scaffold. Workspaces, PRs, and Jira land in later slices.
      </p>
    </main>
  );
}
