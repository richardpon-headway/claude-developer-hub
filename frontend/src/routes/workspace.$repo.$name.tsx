import { Outlet, createFileRoute } from "@tanstack/react-router";

// Layout route for everything under /workspace/{repo}/{name}/. The
// actual page lives in the sibling ``.index.tsx`` child; nested routes
// (e.g. ``files/{...path}``) render through this Outlet.
export const Route = createFileRoute("/workspace/$repo/$name")({
  component: WorkspaceLayout,
});

function WorkspaceLayout() {
  return <Outlet />;
}
