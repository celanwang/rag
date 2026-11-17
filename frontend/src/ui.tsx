import { CircleAlert, LoaderCircle, X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";
import { ApiError, displayError } from "./api";

export function Notice({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div className="notice notice-error" role="alert">
      <CircleAlert size={16} />
      <div>
        {displayError(error)}
        {error instanceof ApiError && error.trace && <small>请求 ID {error.trace}</small>}
      </div>
    </div>
  );
}

export function Busy({ children }: { children: ReactNode }) {
  return <><LoaderCircle size={17} className="spin" />{children}</>;
}

export function PageHeading({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return <header className="page-heading"><div><h1>{title}</h1><p>{children}</p></div>{action}</header>;
}

export function Dialog({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => { ref.current?.showModal(); }, []);
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={id} onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <header className="dialog-heading">
        <h2 id={id}>{title}</h2>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭"><X size={19} /></button>
      </header>
      {children}
    </dialog>
  );
}

export function Empty({ icon, title, children, action }: {
  icon: ReactNode; title: string; children?: ReactNode; action?: ReactNode;
}) {
  return (
    <div className="empty">
      <div className="empty-illustration">{icon}</div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export const UUID_PATTERN = /^[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12}$/i;
