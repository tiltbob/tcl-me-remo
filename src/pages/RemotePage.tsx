// An on-screen remote. Every button sends one key; a key that fails says why, right here.
// There is no power toggle: Wake and Sleep are discrete and safe to press twice.

import { DialogButton, Field, Focusable } from "@decky/ui";
import type { CSSProperties, FC, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import {
  FaArrowLeft,
  FaBars,
  FaChevronDown,
  FaChevronLeft,
  FaChevronRight,
  FaChevronUp,
  FaHome,
  FaVolumeDown,
  FaVolumeMute,
  FaVolumeUp,
} from "react-icons/fa";

import { rpc } from "../backend";
import { ButtonRow, Page, Section } from "../components/common";
import { busyReason, store, useTv } from "../store";
import { colors } from "../text";

// How many presses may wait for the TV before further ones are refused.
const MAX_WAITING = 4;

interface RemoteKey {
  key: string;
  label: ReactNode;
  name: string;
}

const icon: CSSProperties = { display: "block", margin: "0 auto" };

/** An icon with its name underneath: an arrow explains itself, a speaker with an x less so. */
const named = (symbol: ReactNode, text: string): ReactNode => (
  <span style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
    {symbol}
    <span>{text}</span>
  </span>
);

const ROWS: (RemoteKey | null)[][] = [
  [
    { key: "WAKE", label: "Wake", name: "Wake" },
    null,
    { key: "SLEEP", label: "Sleep", name: "Sleep" },
  ],
  [null, { key: "UP", label: <FaChevronUp style={icon} />, name: "Up" }, null],
  [
    { key: "LEFT", label: <FaChevronLeft style={icon} />, name: "Left" },
    { key: "OK", label: "OK", name: "OK" },
    { key: "RIGHT", label: <FaChevronRight style={icon} />, name: "Right" },
  ],
  [null, { key: "DOWN", label: <FaChevronDown style={icon} />, name: "Down" }, null],
  [
    { key: "BACK", label: named(<FaArrowLeft />, "Back"), name: "Back" },
    { key: "HOME", label: named(<FaHome />, "Home"), name: "Home" },
    { key: "MENU", label: named(<FaBars />, "Menu"), name: "Menu" },
  ],
  [
    { key: "VOL_DOWN", label: named(<FaVolumeDown />, "Vol −"), name: "Volume down" },
    { key: "MUTE", label: named(<FaVolumeMute />, "Mute"), name: "Mute" },
    { key: "VOL_UP", label: named(<FaVolumeUp />, "Vol +"), name: "Volume up" },
  ],
  [null, { key: "F3", label: "Source list", name: "Source list (F3)" }, null],
];

const cell: CSSProperties = { width: "9em", minWidth: 0, height: "3.2em", flex: "0 0 auto", padding: "0 8px" };

export const RemotePage: FC = () => {
  const state = useTv();
  const [waiting, setWaiting] = useState(0);
  const [note, setNote] = useState<{ failed: boolean; text: string } | null>(null);
  const chain = useRef<Promise<void>>(Promise.resolve());
  const count = useRef(0);
  const round = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const bluetooth = state.status?.bluetooth;
  const paired = bluetooth?.paired === true;
  const busy = busyReason(state);
  const known = new Set((state.bundle?.keys ?? []).map((entry) => entry.name));

  const press = (entry: RemoteKey) => {
    if (count.current >= MAX_WAITING) {
      setNote({ failed: true, text: "The TV has not taken the earlier keys yet. Wait a moment." });
      return;
    }
    const mine = round.current;
    count.current += 1;
    setWaiting(count.current);
    // One key at a time, in the order they were pressed.
    chain.current = chain.current
      .then(async () => {
        // An earlier key failed: do not keep hammering a TV that is not there.
        if (mine !== round.current) return;
        const reply = await rpc<{ key: string }>("send_key", { key: entry.key });
        if (!reply.ok) round.current += 1;
        if (mounted.current) {
          setNote(
            reply.ok
              ? { failed: false, text: `Sent ${entry.name}` }
              : { failed: true, text: `${entry.name} was not sent: ${reply.error.message}` },
          );
        }
      })
      .catch((reason) => console.warn("tcl-me-remo: send_key handling failed", reason))
      .then(() => {
        count.current -= 1;
        if (mounted.current) setWaiting(count.current);
        if (count.current === 0) store.poll();
      });
  };

  return (
    <Page state={state}>
      <Section title="Remote">
        <Field
          focusable={true}
          label={
            !paired
              ? "No TV is paired"
              : bluetooth?.ready
                ? `${bluetooth.tv_name || "The TV"} is connected`
                : `${bluetooth?.tv_name || "The TV"} is not connected right now`
          }
          description={
            <div>
              <div>
                {!paired
                  ? "Pair a TV on the Setup page first."
                  : bluetooth?.ready
                    ? "Keys are sent as you press them."
                    : "The first key waits for the TV to connect, which can take a few seconds."}
              </div>
              {busy && <div style={{ color: colors.warn }}>The service is busy ({busy}); keys cannot be sent until it is done.</div>}
              <div
                style={{
                  color: note?.failed ? colors.bad : colors.text,
                  minHeight: "1.4em",
                  fontWeight: note?.failed ? "bold" : "normal",
                  overflowWrap: "anywhere",
                }}
              >
                {waiting > 0 && !note?.failed ? "Sending…" : (note?.text ?? "")}
              </div>
            </div>
          }
        />
        <Focusable style={{ display: "flex", flexDirection: "column", gap: "8px", alignItems: "center", padding: "12px 0" }}>
          {ROWS.map((row, index) => (
            <ButtonRow key={index} style={{ flexWrap: "nowrap", justifyContent: "center" }}>
              {row.map((entry, column) =>
                entry && known.has(entry.key) ? (
                  <DialogButton
                    key={entry.key}
                    style={cell}
                    disabled={!paired || busy !== ""}
                    onClick={() => press(entry)}
                  >
                    {entry.label}
                  </DialogButton>
                ) : (
                  <div key={`gap-${column}`} style={{ ...cell, visibility: "hidden" }} />
                ),
              )}
            </ButtonRow>
          ))}
        </Focusable>
        <Field
          focusable={true}
          bottomSeparator="none"
          label="About these keys"
          description="Wake and Sleep do nothing when the TV is already in that state. Source list sends F3, which is reported to open the input list on one TCL model. On an Android TV, any key also wakes the screen."
        />
      </Section>
    </Page>
  );
};
