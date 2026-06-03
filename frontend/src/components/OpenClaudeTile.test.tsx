import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/config");

import * as configApi from "../api/config";

import { OpenClaudeTile } from "./OpenClaudeTile";

function renderTile() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <OpenClaudeTile />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(configApi.openGlobalClaude).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("OpenClaudeTile", () => {
  test("renders the labeled tile", () => {
    renderTile();
    expect(screen.getByRole("heading", { name: /open claude terminal/i })).toBeInTheDocument();
  });

  test("Open Claude button fires the blank-session api (no prompt needed)", async () => {
    vi.mocked(configApi.openGlobalClaude).mockResolvedValue({ spawned: true });

    renderTile();
    const btn = screen.getByRole("button", { name: /^open claude$/i });
    expect(btn).toBeEnabled();
    fireEvent.click(btn);

    await waitFor(() => {
      expect(configApi.openGlobalClaude).toHaveBeenCalledTimes(1);
    });
  });
});
