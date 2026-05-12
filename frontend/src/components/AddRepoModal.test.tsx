import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { AddRepoModal } from "./AddRepoModal";
import { ApiError } from "../api/client";
import * as reposApi from "../api/repos";

vi.mock("../api/repos");

function renderModal(overrides: Partial<React.ComponentProps<typeof AddRepoModal>> = {}) {
  const onClose = overrides.onClose ?? vi.fn();
  const onSaved = overrides.onSaved ?? vi.fn();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <AddRepoModal open={overrides.open ?? true} onClose={onClose} onSaved={onSaved} />
    </QueryClientProvider>,
  );
  return { ...utils, onClose, onSaved };
}

beforeEach(() => {
  vi.mocked(reposApi.onboardRepo).mockReset();
  vi.mocked(reposApi.getOnboardStatus).mockReset();
  vi.mocked(reposApi.listRepoCandidates).mockReset();
  // Default: no candidates so existing tests don't have to think about
  // the list section. Candidate-specific tests override this.
  vi.mocked(reposApi.listRepoCandidates).mockResolvedValue([]);
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
  });
});

afterEach(() => {
  cleanup();
});

describe("AddRepoModal", () => {
  test("renders the form when no session yet", () => {
    renderModal();
    expect(screen.getByPlaceholderText(/development\/some-repo/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /inspect/i })).toBeDisabled();
  });

  test("renders candidate cards from /api/repos/candidates", async () => {
    vi.mocked(reposApi.listRepoCandidates).mockResolvedValue([
      { path: "/Users/x/dev/foo", name: "foo", already_configured: false },
      { path: "/Users/x/dev/bar", name: "bar", already_configured: true },
    ]);
    renderModal();
    expect(await screen.findByText("foo")).toBeInTheDocument();
    expect(screen.getByText("bar")).toBeInTheDocument();
    expect(screen.getByText(/already added/i)).toBeInTheDocument();
  });

  test("clicking an enabled candidate calls onboardRepo with its path", async () => {
    vi.mocked(reposApi.listRepoCandidates).mockResolvedValue([
      { path: "/Users/x/dev/foo", name: "foo", already_configured: false },
    ]);
    vi.mocked(reposApi.onboardRepo).mockResolvedValue({
      session_id: "sid-x",
      prompt: "stub",
    });
    // Block the status poll so we stay in awaiting_claude.
    vi.mocked(reposApi.getOnboardStatus).mockImplementation(
      () => new Promise(() => {}),
    );
    renderModal();
    const card = await screen.findByRole("button", { name: /foo/i });
    fireEvent.click(card);
    await waitFor(() =>
      expect(reposApi.onboardRepo).toHaveBeenCalledWith("/Users/x/dev/foo"),
    );
  });

  test("already-configured candidate is rendered disabled", async () => {
    vi.mocked(reposApi.listRepoCandidates).mockResolvedValue([
      { path: "/Users/x/dev/foo", name: "foo", already_configured: true },
    ]);
    renderModal();
    const card = await screen.findByRole("button", { name: /foo/i });
    expect(card).toBeDisabled();
    // Click shouldn't trigger onboardRepo
    fireEvent.click(card);
    await new Promise((r) => setTimeout(r, 50));
    expect(reposApi.onboardRepo).not.toHaveBeenCalled();
  });

  test("submit calls onboardRepo and renders the returned prompt", async () => {
    vi.mocked(reposApi.onboardRepo).mockResolvedValue({
      session_id: "sid-123",
      prompt: "DO INSPECTION X",
    });
    // Block the status poll on first call so the modal stays in awaiting_claude.
    vi.mocked(reposApi.getOnboardStatus).mockImplementation(
      () => new Promise(() => {}),
    );
    renderModal();

    fireEvent.change(screen.getByPlaceholderText(/development\/some-repo/i), {
      target: { value: "/Users/x/dev/foo" },
    });
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));

    await waitFor(() =>
      expect(reposApi.onboardRepo).toHaveBeenCalledWith("/Users/x/dev/foo"),
    );
    await waitFor(() =>
      expect(screen.getByText(/DO INSPECTION X/)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
    expect(screen.getByText(/waiting for claude/i)).toBeInTheDocument();
  });

  test("renders ApiError detail when onboard fails", async () => {
    vi.mocked(reposApi.onboardRepo).mockRejectedValue(
      new ApiError(400, "path is not a git repository"),
    );
    renderModal();

    fireEvent.change(screen.getByPlaceholderText(/development\/some-repo/i), {
      target: { value: "/nope" },
    });
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/not a git repository/i),
    );
  });

  test("on status=saved, calls onSaved and onClose", async () => {
    vi.mocked(reposApi.onboardRepo).mockResolvedValue({
      session_id: "sid-456",
      prompt: "X",
    });
    vi.mocked(reposApi.getOnboardStatus).mockResolvedValue({
      session_id: "sid-456",
      state: "saved",
      proposed_entry: null,
      error: null,
    });
    const onClose = vi.fn();
    const onSaved = vi.fn();
    renderModal({ onClose, onSaved });

    fireEvent.change(screen.getByPlaceholderText(/development\/some-repo/i), {
      target: { value: "/x" },
    });
    fireEvent.click(screen.getByRole("button", { name: /inspect/i }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });
});
