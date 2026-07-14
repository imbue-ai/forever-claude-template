import { describe, expect, it } from "vitest";
import { chooseInitialLayout, type LayoutInfo } from "./WorkspaceLayouts";

const DESKTOP: LayoutInfo = { slug: "desktop", display_name: "desktop", has_content: true };
const MOBILE: LayoutInfo = { slug: "mobile", display_name: "mobile", has_content: false };
const CUSTOM: LayoutInfo = { slug: "my-setup", display_name: "My Setup", has_content: true };

describe("chooseInitialLayout", () => {
  it("prefers the browser's stored choice when it still exists", () => {
    expect(chooseInitialLayout([DESKTOP, MOBILE, CUSTOM], "my-setup", "mobile")).toBe(CUSTOM);
  });

  it("falls back to the device-kind default when the stored choice is gone", () => {
    expect(chooseInitialLayout([DESKTOP, MOBILE], "deleted-layout", "mobile")).toBe(MOBILE);
    expect(chooseInitialLayout([DESKTOP, MOBILE], "", "desktop")).toBe(DESKTOP);
  });

  it("picks the device-kind default on a first-ever connect", () => {
    expect(chooseInitialLayout([DESKTOP, MOBILE], "", "mobile")).toBe(MOBILE);
  });

  it("falls back to the first layout when the device default is missing", () => {
    expect(chooseInitialLayout([CUSTOM], "", "mobile")).toBe(CUSTOM);
    expect(chooseInitialLayout([CUSTOM], "", "desktop")).toBe(CUSTOM);
  });

  it("returns null when no layouts exist", () => {
    expect(chooseInitialLayout([], "anything", "desktop")).toBeNull();
  });
});
