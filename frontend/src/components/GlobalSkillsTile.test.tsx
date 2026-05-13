import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as RadixTooltip from "@radix-ui/react-tooltip";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("../api/config");

import * as configApi from "../api/config";

import { GlobalSkillsTile } from "./GlobalSkillsTile";

function renderTile() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RadixTooltip.Provider>
        <GlobalSkillsTile />
      </RadixTooltip.Provider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(configApi.getGlobalSkills).mockReset();
  vi.mocked(configApi.runGlobalSkill).mockReset();
});

afterEach(() => {
  cleanup();
});

describe("GlobalSkillsTile", () => {
  test("renders nothing when no global skills are configured", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([]);
    const { container } = renderTile();
    // Give the query a tick to resolve, then assert no tile rendered.
    await waitFor(() => {
      expect(configApi.getGlobalSkills).toHaveBeenCalled();
    });
    expect(container.querySelector("section")).toBeNull();
  });

  test("renders one button per configured skill and clicking fires the api", async () => {
    vi.mocked(configApi.getGlobalSkills).mockResolvedValue([
      {
        name: "pr-check-action-required",
        label: "Check action required",
        description: "Open PRs needing attention",
        cwd: "home",
      },
    ]);
    vi.mocked(configApi.runGlobalSkill).mockResolvedValue({
      window_id: "W",
      claude_session_id: "S",
    });

    renderTile();
    const btn = await screen.findByRole("button", { name: /check action required/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(configApi.runGlobalSkill).toHaveBeenCalledWith(
        "pr-check-action-required",
      );
    });
  });
});
