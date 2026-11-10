// Dialogs. Each one always has a focusable button that closes it, and each one cleans up
// after itself when it goes away, however that happens (a button, B, or Steam closing it).

import {
  ConfirmModal,
  DialogBodyText,
  DialogButton,
  DialogFooter,
  DialogHeader,
  Focusable,
  ModalRoot,
  TextField,
  ToggleField,
} from "@decky/ui";
import type { FC, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { forgetTv, removeService } from "../actions";
import { rpc } from "../backend";
import { recordKind, store, useTv } from "../store";
import { actionName, clockTime, colors, kindColor, kindWord, seconds, triggerName } from "../text";
import type { ActionRecord, BluetoothState, WifiStatus } from "../types";
import { Steps } from "./common";

const footerButton = { width: "auto", minWidth: "9em", flex: "0 0 auto" };

const Footer: FC<{ children?: ReactNode }> = ({ children }) => (
  <DialogFooter>
    <Focusable flow-children="right" style={{ display: "flex", gap: "10px", justifyContent: "flex-end" }}>
      {children}
    </Focusable>
  </DialogFooter>
);

const paragraph = { margin: "0 0 10px 0" };

// ---------------------------------------------------------------------- an action's trace

export const RecordModal: FC<{ closeModal?: () => void; record: ActionRecord }> = ({ closeModal, record }) => {
  const kind = recordKind(record);
  return (
    <ConfirmModal
      strTitle={<span style={{ color: kindColor[kind] }}>{`${actionName(record)}: ${kindWord[kind]}`}</span>}
      strOKButtonText="Close"
      bAlertDialog={true}
      closeModal={closeModal}
    >
      <div style={{ overflowWrap: "anywhere" }}>{record.detail || "No detail was given."}</div>
      <div style={{ color: colors.dim }}>
        {triggerName(record)} · {clockTime(record.time)} · took {seconds(record.seconds)}
      </div>
      <Steps steps={record.steps} />
    </ConfirmModal>
  );
};

// ---------------------------------------------------------------------- Bluetooth pairing

type PairPhase = "starting" | "open" | "paired" | "closed" | "failed";

function minutes(secondsLeft: number): string {
  const whole = Math.max(0, Math.round(secondsLeft));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/**
 * Opens pairing mode, tells the user what to do on the TV, and follows the service's status
 * until a TV is paired or pairing mode closes. Closing the dialog closes pairing mode.
 */
export const PairModal: FC<{ closeModal?: () => void }> = ({ closeModal }) => {
  const state = useTv();
  const [phase, setPhase] = useState<PairPhase>("starting");
  const [message, setMessage] = useState("");
  const phaseNow = useRef<PairPhase>(phase);
  phaseNow.current = phase;
  const mounted = useRef(true);
  const openedAt = useRef(0);
  const sawOpen = useRef(false);
  const pairedBefore = useRef(false);

  const start = () => {
    setPhase("starting");
    setMessage("");
    sawOpen.current = false;
    pairedBefore.current = store.getState().status?.bluetooth?.paired === true;
    void rpc<BluetoothState>("pair_start").then((reply) => {
      if (!mounted.current) {
        // Closed while the request was on its way: do not leave pairing mode open.
        if (reply.ok) void rpc("pair_cancel");
        return;
      }
      if (!reply.ok) {
        setMessage(reply.error.message);
        setPhase("failed");
        return;
      }
      openedAt.current = Date.now();
      setPhase("open");
      store.poll();
    });
  };

  useEffect(() => {
    mounted.current = true;
    start();
    return () => {
      mounted.current = false;
      if (phaseNow.current === "open") void rpc("pair_cancel").then(() => store.poll());
    };
  }, []);

  const bluetooth = state.status?.bluetooth;
  useEffect(() => {
    if (phase !== "open" || !bluetooth) return;
    if (bluetooth.pairing) {
      sawOpen.current = true;
      return;
    }
    // A status that was requested before pairing mode opened may still arrive; only trust
    // "not pairing" once pairing mode has been seen open, or after a moment.
    const settled = sawOpen.current || Date.now() - openedAt.current > 2500;
    if (bluetooth.paired && (settled || !pairedBefore.current)) {
      setPhase("paired");
      void store.loadConfig();
    } else if (!bluetooth.paired && settled) {
      setMessage(bluetooth.error || "Pairing mode closed before a TV paired.");
      setPhase("closed");
    }
  }, [phase, bluetooth?.pairing, bluetooth?.paired, state.status?.uptime]);

  const close = () => closeModal?.();
  const name = state.config?.bluetooth.device_name || "SteamOS Remote";

  return (
    <ModalRoot closeModal={close} onCancel={close}>
      <DialogHeader>Pair TV</DialogHeader>
      <DialogBodyText>
        {phase === "starting" && <p style={paragraph}>Opening pairing mode.</p>}
        {phase === "open" && (
          <div>
            <p style={paragraph}>
              Pairing mode is open
              {bluetooth?.pairing ? ` for another ${minutes(bluetooth.pairing_seconds_left)}` : ""}. On the TV:
            </p>
            <ol style={{ margin: "0 0 10px 0", paddingLeft: "1.4em" }}>
              <li>Open Settings &gt; Remotes &amp; Accessories &gt; Pair accessory (older firmware: Add accessory).</li>
              <li>
                Pick <b>{name}</b> in the list and confirm.
              </li>
            </ol>
            {bluetooth?.pairing && bluetooth.error && (
              // What the service has to say about the open pairing mode matters now, not after
              // it has timed out: for one, that another Bluetooth pairing screen is in the way.
              <p style={{ ...paragraph, color: colors.warn, overflowWrap: "anywhere" }}>
                Bluetooth problem: {bluetooth.error}
              </p>
            )}
            <p style={{ ...paragraph, color: colors.dim }}>
              If this machine does not show up: check that Bluetooth is on in SteamOS, move closer, and on the TV
              remove any earlier pairing with this machine first.
            </p>
            {!state.status && state.started && (
              <p style={{ ...paragraph, color: colors.bad }}>The background service stopped answering.</p>
            )}
          </div>
        )}
        {phase === "paired" && (
          <div>
            <p style={{ ...paragraph, color: colors.ok }}>Paired with {bluetooth?.tv_name || "the TV"}.</p>
            <p style={paragraph}>
              Pair once; the pairing survives restarts of both sides. Next, check that it works: turn the TV off and
              on again from the panel.
            </p>
          </div>
        )}
        {(phase === "closed" || phase === "failed") && (
          <div>
            <p style={{ ...paragraph, color: colors.bad }}>
              {phase === "failed" ? "Pairing mode could not be opened." : "No TV was paired."}
            </p>
            <p style={{ ...paragraph, overflowWrap: "anywhere" }}>{message}</p>
          </div>
        )}
      </DialogBodyText>
      <Footer>
        {(phase === "closed" || phase === "failed") && (
          <DialogButton style={footerButton} onClick={() => start()}>
            Try again
          </DialogButton>
        )}
        <DialogButton style={footerButton} onClick={close}>
          {phase === "paired" ? "Done" : phase === "starting" || phase === "open" ? "Cancel pairing" : "Close"}
        </DialogButton>
      </Footer>
    </ModalRoot>
  );
};

// ---------------------------------------------------------------------- confirmations

export const ForgetModal: FC<{ closeModal?: () => void; tvName: string; remoteName: string }> = ({
  closeModal,
  tvName,
  remoteName,
}) => (
  <ConfirmModal
    strTitle={`Forget ${tvName || "the TV"}?`}
    strOKButtonText="Forget TV"
    strCancelButtonText="Keep"
    bDestructiveWarning={true}
    onOK={() => void forgetTv()}
    closeModal={closeModal}
  >
    <p style={paragraph}>This removes the pairing on this machine. The TV is no longer controlled.</p>
    <p style={paragraph}>
      Also remove <b>{remoteName || "SteamOS Remote"}</b> on the TV, under Settings &gt; Remotes &amp; Accessories.
      Both sides have to forget before they can be paired again.
    </p>
  </ConfirmModal>
);

export const RemoveServiceModal: FC<{ closeModal?: () => void }> = ({ closeModal }) => {
  const [purge, setPurge] = useState(false);
  return (
    <ConfirmModal
      strTitle="Remove the background service?"
      strOKButtonText={purge ? "Remove service and settings" : "Remove service"}
      strCancelButtonText="Keep"
      bDestructiveWarning={true}
      onOK={() => void removeService(purge)}
      closeModal={closeModal}
    >
      <p style={paragraph}>
        The service is stopped and removed. The TV is no longer turned on or off with this machine. The plugin
        installs the service again the next time it loads; to remove it for good, uninstall the plugin in Decky.
      </p>
      <ToggleField
        label="Also delete settings"
        description="Deletes the settings and what was learned about the TV. On the TV, remove this remote under Remotes & Accessories as well."
        checked={purge}
        onChange={setPurge}
      />
    </ConfirmModal>
  );
};

/** Shown before input switching is turned on: what will be sent to the TV, and when. */
export const ConfirmSendModal: FC<{
  closeModal?: () => void;
  title: string;
  okLabel: string;
  /** Why this cannot be turned on yet ("" when it can). */
  blocked: string;
  /** True while what will be sent is still being looked up; it cannot be agreed to meanwhile. */
  waiting?: boolean;
  children?: ReactNode;
  onOK: () => void;
}> = ({ closeModal, title, okLabel, blocked, waiting = false, children, onOK }) => (
  <ConfirmModal
    strTitle={title}
    strOKButtonText={okLabel}
    strCancelButtonText="Cancel"
    bOKDisabled={blocked !== "" || waiting}
    onOK={() => {
      if (!blocked && !waiting) onOK();
    }}
    closeModal={closeModal}
  >
    {children}
    {blocked && !waiting && <p style={{ ...paragraph, color: colors.bad }}>{blocked}</p>}
  </ConfirmModal>
);

// ---------------------------------------------------------------------- Wi-Fi pairing

type WifiPhase = "starting" | "code" | "finishing" | "done" | "failed";

/**
 * Pairs the Wi-Fi assist: asks the TV to show a code, takes the code, finishes. Closing the
 * dialog before the end cancels the pairing on the service.
 */
export const WifiPairModal: FC<{ closeModal?: () => void; host: string; name?: string }> = ({
  closeModal,
  host,
  name,
}) => {
  const [phase, setPhase] = useState<WifiPhase>("starting");
  const [message, setMessage] = useState("");
  const [code, setCode] = useState("");
  const phaseNow = useRef<WifiPhase>(phase);
  phaseNow.current = phase;
  const mounted = useRef(true);

  const start = () => {
    setPhase("starting");
    setMessage("");
    setCode("");
    void rpc<WifiStatus>("wifi_pair_start", { host }).then((reply) => {
      if (!mounted.current) {
        if (reply.ok) void rpc("wifi_pair_cancel");
        return;
      }
      if (reply.ok) {
        setPhase("code");
      } else {
        setMessage(reply.error.message);
        setPhase("failed");
      }
    });
  };

  const finish = () => {
    const entered = code.trim();
    if (!entered || phaseNow.current !== "code") return;
    setPhase("finishing");
    const params: Record<string, unknown> = name ? { code: entered, name } : { code: entered };
    void rpc<WifiStatus>("wifi_pair_finish", params).then((reply) => {
      if (reply.ok) void store.loadConfig();
      if (!mounted.current) {
        // Closed while the code was being checked. A code the TV turned down leaves the
        // pairing open on the service (it may be tried again), so close it here.
        if (reply.ok) store.poll();
        else void rpc("wifi_pair_cancel").then(() => store.poll());
        return;
      }
      store.poll();
      if (reply.ok) {
        setPhase("done");
      } else {
        setMessage(reply.error.message);
        setPhase("failed");
      }
    });
  };

  useEffect(() => {
    mounted.current = true;
    start();
    return () => {
      mounted.current = false;
      const now = phaseNow.current;
      // "failed" included: the service may still hold a half-open pairing with the TV.
      if (now === "code" || now === "failed") void rpc("wifi_pair_cancel").then(() => store.poll());
    };
  }, []);

  const close = () => closeModal?.();
  const tv = name || host;

  return (
    <ModalRoot closeModal={close} onCancel={close}>
      <DialogHeader>Pair over Wi-Fi</DialogHeader>
      <DialogBodyText>
        {phase === "starting" && <p style={paragraph}>Asking {tv} to show a pairing code.</p>}
        {(phase === "code" || phase === "finishing") && (
          <div>
            <p style={paragraph}>The TV shows a six-character code. Enter it here.</p>
            <TextField
              label="Code on the TV"
              value={code}
              focusOnMount={true}
              bShowClearAction={true}
              disabled={phase === "finishing"}
              onChange={(event) => setCode(event.target.value)}
            />
          </div>
        )}
        {phase === "done" && (
          <div>
            <p style={{ ...paragraph, color: colors.ok }}>Paired with {tv} over Wi-Fi.</p>
            <p style={paragraph}>The Wi-Fi assist is now turned on.</p>
          </div>
        )}
        {phase === "failed" && (
          <div>
            <p style={{ ...paragraph, color: colors.bad }}>Pairing over Wi-Fi did not work.</p>
            <p style={{ ...paragraph, overflowWrap: "anywhere" }}>{message}</p>
          </div>
        )}
      </DialogBodyText>
      <Footer>
        {(phase === "code" || phase === "finishing") && (
          <DialogButton style={footerButton} disabled={phase === "finishing" || !code.trim()} onClick={() => finish()}>
            {phase === "finishing" ? "Pairing…" : "Finish pairing"}
          </DialogButton>
        )}
        {phase === "failed" && (
          <DialogButton style={footerButton} onClick={() => start()}>
            Start again
          </DialogButton>
        )}
        <DialogButton style={footerButton} onClick={close}>
          {phase === "done" ? "Done" : phase === "failed" ? "Close" : "Cancel"}
        </DialogButton>
      </Footer>
    </ModalRoot>
  );
};
