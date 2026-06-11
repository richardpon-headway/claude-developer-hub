import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { hasUrl, linkify } from "./linkify";

describe("linkify", () => {
  test("wraps a URL in an anchor and keeps surrounding text", () => {
    render(<div>{linkify("see https://example.com/pr here")}</div>);
    const link = screen.getByRole("link", { name: "https://example.com/pr" });
    expect(link).toHaveAttribute("href", "https://example.com/pr");
    expect(link).toHaveAttribute("target", "_blank");
    expect(screen.getByText(/see/)).toBeInTheDocument();
    expect(screen.getByText(/here/)).toBeInTheDocument();
  });

  test("strips trailing sentence punctuation from the link", () => {
    render(<div>{linkify("done at https://example.com.")}</div>);
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "https://example.com",
    );
  });

  test("renders multiple URLs", () => {
    render(
      <div>{linkify("https://a.com and https://b.com")}</div>,
    );
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  test("plain text with no URL produces no links", () => {
    render(<div>{linkify("just a normal task")}</div>);
    expect(screen.queryByRole("link")).toBeNull();
  });

  test("hasUrl detects presence", () => {
    expect(hasUrl("ping https://x.com")).toBe(true);
    expect(hasUrl("no links here")).toBe(false);
  });
});
