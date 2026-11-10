import { DialogButton, Focusable, Navigation, staticClasses } from "@decky/ui";
import type { FC } from "react";
import { FaCog } from "react-icons/fa";

import { ROUTE } from "../actions";

/** Open a page of the full-screen settings; the Quick Access menu closes after navigating. */
export function openSettings(page: string): void {
  try {
    Navigation.Navigate(`${ROUTE}/${page}`);
    Navigation.CloseSideMenus();
  } catch (reason) {
    console.warn("tcl-me-remo: could not open the settings page", reason);
  }
}

export const TitleView: FC = () => (
  <Focusable
    style={{
      display: "flex",
      padding: "0",
      width: "100%",
      boxShadow: "none",
      alignItems: "center",
      justifyContent: "space-between",
    }}
    className={staticClasses.Title}
  >
    <div>TV remote</div>
    <DialogButton
      style={{ height: "28px", width: "40px", minWidth: 0, padding: "10px 12px" }}
      onClick={() => openSettings("setup")}
    >
      <FaCog style={{ marginTop: "-4px", display: "block" }} />
    </DialogButton>
  </Focusable>
);
