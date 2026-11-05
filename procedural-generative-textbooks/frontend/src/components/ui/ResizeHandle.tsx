import { useCallback, useRef, type KeyboardEvent, type PointerEvent } from "react";

interface Props {
  /** "col" resizes widths (vertical bar), "row" resizes heights (horizontal bar). */
  axis: "col" | "row";
  /** Current size of the pane being resized, px. */
  size: number;
  /** +1 when dragging right/down grows the pane, −1 when it shrinks it. */
  direction: 1 | -1;
  onResize: (px: number) => void;
  label: string;
}

/** A 1px hairline with a wider invisible hit area; pointer and keyboard resizable. */
export function ResizeHandle({ axis, size, direction, onResize, label }: Props) {
  const start = useRef<{ pos: number; size: number } | null>(null);

  const onPointerDown = useCallback(
    (e: PointerEvent<HTMLDivElement>) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      start.current = { pos: axis === "col" ? e.clientX : e.clientY, size };
      document.body.style.cursor = axis === "col" ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
    },
    [axis, size],
  );

  const onPointerMove = useCallback(
    (e: PointerEvent<HTMLDivElement>) => {
      if (!start.current) return;
      const pos = axis === "col" ? e.clientX : e.clientY;
      onResize(start.current.size + (pos - start.current.pos) * direction);
    },
    [axis, direction, onResize],
  );

  const end = useCallback(() => {
    start.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 48 : 16;
    const grow = axis === "col" ? ["ArrowRight", "ArrowLeft"] : ["ArrowDown", "ArrowUp"];
    if (e.key === grow[0]) onResize(size + step * direction);
    else if (e.key === grow[1]) onResize(size - step * direction);
    else return;
    e.preventDefault();
  };

  const isCol = axis === "col";
  return (
    <div
      role="separator"
      aria-orientation={isCol ? "vertical" : "horizontal"}
      aria-label={label}
      aria-valuenow={size}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onKeyDown={onKeyDown}
      className={`group relative z-20 shrink-0 bg-line focus-visible:bg-accent ${
        isCol ? "w-px cursor-col-resize" : "h-px cursor-row-resize"
      }`}
    >
      <div className={`absolute ${isCol ? "-left-[3px] -right-[3px] inset-y-0" : "-top-[3px] -bottom-[3px] inset-x-0"} group-hover:bg-accent/40`} />
    </div>
  );
}
