import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { EditableText } from "./EditableText";

afterEach(() => cleanup());

describe("EditableText", () => {
  test("renders read-only text with linkified URLs until clicked", () => {
    render(
      <EditableText
        value="ping https://example.com"
        placeholder="add…"
        onSave={() => {}}
      />,
    );
    // Link is present in display mode.
    expect(
      screen.getByRole("link", { name: "https://example.com" }),
    ).toBeInTheDocument();
    // No textarea until the text is clicked.
    expect(screen.queryByRole("textbox")).toBeNull();

    fireEvent.click(screen.getByText(/ping/));
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  test("Enter commits and exits edit mode", () => {
    const onSave = vi.fn();
    render(
      <EditableText value="" placeholder="add…" autoEdit onSave={onSave} />,
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "buy milk" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSave).toHaveBeenCalledWith("buy milk");
    // Back to display mode.
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  test("Shift+Enter does not commit — stays in edit mode", () => {
    const onSave = vi.fn();
    render(
      <EditableText value="" placeholder="add…" autoEdit onSave={onSave} />,
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "line one" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    // Still editing; Enter+Shift didn't blur/commit.
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  test("Escape reverts the draft and exits edit mode", () => {
    const onSave = vi.fn();
    render(
      <EditableText
        value="original"
        placeholder="add…"
        onSave={onSave}
      />,
    );
    fireEvent.click(screen.getByText("original"));
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "changed" } });
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByText("original")).toBeInTheDocument();
  });
});
