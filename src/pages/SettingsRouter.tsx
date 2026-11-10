import { SidebarNavigation } from "@decky/ui";
import type { SidebarNavigationProps } from "@decky/ui";
import type { FC } from "react";
import { useMemo } from "react";

import { ROUTE } from "../actions";
import { useHold } from "../store";
import { AutomationPage } from "./AutomationPage";
import { BluetoothPage } from "./BluetoothPage";
import { DiagnosticsPage } from "./DiagnosticsPage";
import { InputPage } from "./InputPage";
import { RemotePage } from "./RemotePage";
import { SetupPage } from "./SetupPage";
import { WifiPage } from "./WifiPage";

/**
 * The full-screen settings. Holds the status polling for as long as it is on screen, without
 * subscribing to it: each page listens for itself, and Steam's SidebarNavigation is handed
 * the same list of pages on every render instead of a new one each second.
 */
export const SettingsRouter: FC = () => {
  useHold();
  const pages = useMemo<SidebarNavigationProps["pages"]>(
    () => [
      { title: "Setup", content: <SetupPage />, route: `${ROUTE}/setup` },
      { title: "Automation", content: <AutomationPage />, route: `${ROUTE}/automation` },
      { title: "Input", content: <InputPage />, route: `${ROUTE}/input` },
      { title: "Remote", content: <RemotePage />, route: `${ROUTE}/remote` },
      "separator",
      { title: "Bluetooth", content: <BluetoothPage />, route: `${ROUTE}/bluetooth` },
      { title: "Wi-Fi assist", content: <WifiPage />, route: `${ROUTE}/wifi` },
      { title: "Diagnostics", content: <DiagnosticsPage />, route: `${ROUTE}/diagnostics` },
    ],
    [],
  );
  return <SidebarNavigation title="TV remote" showTitle={true} pages={pages} />;
};
