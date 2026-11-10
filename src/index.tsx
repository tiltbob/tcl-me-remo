import { definePlugin, routerHook } from "@decky/api";
import { FaTv } from "react-icons/fa";

import { ROUTE } from "./actions";
import { QuickPanel } from "./components/QuickPanel";
import { TitleView } from "./components/TitleView";
import { SettingsRouter } from "./pages/SettingsRouter";
import { store } from "./store";

export default definePlugin(() => {
  // Runs once per load of the Steam UI. The panel and the settings page start and stop the
  // status polling themselves when they mount and unmount; nothing runs in between.
  routerHook.addRoute(ROUTE, SettingsRouter);

  return {
    name: "tcl-me-remo",
    titleView: <TitleView />,
    content: <QuickPanel />,
    icon: <FaTv />,
    onDismount() {
      routerHook.removeRoute(ROUTE);
      store.shutdown();
    },
  };
});
