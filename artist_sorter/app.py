from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import load_config, managed_db_path, save_config
from .core import PlanItem, download_database, execute_plan, make_plan, validate_violet_db


class ArtistSorterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Artist Sorter")
        self.geometry("1180x800")
        self.minsize(980, 660)

        self.config_data = load_config()
        self.plan: list[PlanItem] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()

        default_db = Path(self.config_data.get("db_path", str(managed_db_path())))
        default_db_folder = self.config_data.get("db_folder", str(default_db.parent))

        self.source_var = tk.StringVar(value=self.config_data.get("source_dir", ""))
        self.output_var = tk.StringVar(value=self.config_data.get("output_dir", ""))
        self.db_var = tk.StringVar(value=str(default_db))
        self.db_folder_var = tk.StringVar(value=default_db_folder)
        self.url_var = tk.StringVar(value=self.config_data.get("db_url", ""))
        self.mode_var = tk.StringVar(value=self.config_data.get("mode", "move"))
        self.artist_strategy_var = tk.StringVar(value=self.config_data.get("artist_strategy", "first"))
        self.status_var = tk.StringVar(value="준비됨")

        self._build_ui()
        self.after(100, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        self._path_row(outer, 0, "원본 폴더", self.source_var, self._pick_source)
        self._path_row(outer, 1, "정리 폴더", self.output_var, self._pick_output)
        self._path_row(outer, 2, "Violet DB", self.db_var, self._pick_db)
        self._path_row(outer, 3, "DB 저장 폴더", self.db_folder_var, self._pick_db_folder)

        ttk.Label(outer, text="Pi DB URL").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(outer, textvariable=self.url_var).grid(row=4, column=1, sticky="ew", padx=(8, 8), pady=4)
        self.download_btn = ttk.Button(outer, text="DB 갱신", command=self._download_db)
        self.download_btn.grid(row=4, column=2, sticky="ew", pady=4)

        options = ttk.Frame(outer)
        options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        ttk.Label(options, text="실행 방식").pack(side="left")
        ttk.Radiobutton(options, text="이동", variable=self.mode_var, value="move").pack(side="left", padx=(8, 4))
        ttk.Radiobutton(options, text="복사", variable=self.mode_var, value="copy").pack(side="left", padx=4)
        ttk.Separator(options, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(options, text="작가가 여러 명일 때").pack(side="left")
        ttk.Radiobutton(options, text="첫 번째 작가", variable=self.artist_strategy_var, value="first").pack(side="left", padx=(8, 4))
        ttk.Radiobutton(options, text="작가명 합치기", variable=self.artist_strategy_var, value="combined").pack(side="left", padx=4)

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        self.preview_btn = ttk.Button(actions, text="1. 미리보기", command=self._preview)
        self.preview_btn.pack(side="left")
        self.run_btn = ttk.Button(actions, text="2. 정리 실행", command=self._run_plan, state="disabled")
        self.run_btn.pack(side="left", padx=8)
        ttk.Label(actions, textvariable=self.status_var).pack(side="right")

        table_frame = ttk.Frame(outer)
        table_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = ("id", "source", "artists", "title", "destination", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        labels = {
            "id": "ID", "source": "원본", "artists": "작가", "title": "제목",
            "destination": "대상", "status": "상태",
        }
        widths = {"id": 90, "source": 180, "artists": 180, "title": 260, "destination": 260, "status": 120}
        for col in columns:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], minwidth=70, stretch=col in {"source", "artists", "title", "destination"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0))

    def _path_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=4)
        ttk.Button(parent, text="찾아보기", command=command).grid(row=row, column=2, sticky="ew", pady=4)

    def _pick_source(self) -> None:
        value = filedialog.askdirectory(title="히토미 다운로드 폴더 선택")
        if value:
            self.source_var.set(value)
            if not self.output_var.get():
                self.output_var.set(str(Path(value).parent / "sorted-by-artist"))

    def _pick_output(self) -> None:
        value = filedialog.askdirectory(title="정리 결과 폴더 선택")
        if value:
            self.output_var.set(value)

    def _pick_db(self) -> None:
        value = filedialog.askopenfilename(
            title="Violet SQLite DB 선택",
            filetypes=[("SQLite DB", "*.db"), ("모든 파일", "*.*")],
        )
        if value:
            try:
                validate_violet_db(Path(value))
            except Exception as exc:
                messagebox.showerror("DB 확인 실패", str(exc))
                return
            path = Path(value)
            self.db_var.set(str(path))
            self.db_folder_var.set(str(path.parent))

    def _pick_db_folder(self) -> None:
        current = self.db_folder_var.get().strip()
        value = filedialog.askdirectory(
            title="DB 저장 폴더 선택",
            initialdir=current if current and Path(current).is_dir() else None,
        )
        if value:
            self.db_folder_var.set(value)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.preview_btn.configure(state=state)
        self.download_btn.configure(state=state)
        self.run_btn.configure(state="disabled" if busy or not self.plan else "normal")
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()
        if text:
            self.status_var.set(text)

    def _preview(self) -> None:
        try:
            source = Path(self.source_var.get())
            output = Path(self.output_var.get())
            db_path = Path(self.db_var.get())
            validate_violet_db(db_path)
            if not source.is_dir():
                raise FileNotFoundError("원본 폴더를 선택해주세요.")
            if not str(output):
                raise ValueError("정리 폴더를 선택해주세요.")
        except Exception as exc:
            messagebox.showerror("확인 필요", str(exc))
            return

        self._set_busy(True, "스캔 중...")
        threading.Thread(target=self._preview_worker, args=(source, output, db_path), daemon=True).start()

    def _preview_worker(self, source: Path, output: Path, db_path: Path) -> None:
        try:
            plan = make_plan(source, output, db_path, self.artist_strategy_var.get())
            self.events.put(("preview_done", plan))
        except Exception as exc:
            self.events.put(("error", exc))

    def _download_db(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror("URL 필요", "Pi의 DB 다운로드 URL을 입력해주세요.")
            return

        folder_text = self.db_folder_var.get().strip()
        if not folder_text:
            messagebox.showerror("저장 폴더 필요", "DB 저장 폴더를 선택해주세요.")
            return

        destination = Path(folder_text).expanduser() / "rawdata-korean.db"
        self._set_busy(True, "DB 갱신 중...")
        threading.Thread(target=self._download_worker, args=(url, destination), daemon=True).start()

    def _download_worker(self, url: str, destination: Path) -> None:
        try:
            path = download_database(url, destination)
            self.events.put(("download_done", path))
        except Exception as exc:
            self.events.put(("error", exc))

    def _run_plan(self) -> None:
        ready = sum(1 for x in self.plan if x.status == "준비")
        if ready == 0:
            messagebox.showinfo("실행할 항목 없음", "미리보기 결과에서 실행 가능한 항목이 없습니다.")
            return
        mode_label = "이동" if self.mode_var.get() == "move" else "복사"
        if not messagebox.askyesno("정리 실행", f"{ready}개 항목을 {mode_label}할까요?\n이미 존재하는 대상은 덮어쓰지 않습니다."):
            return
        self._set_busy(True, f"{mode_label} 중...")
        threading.Thread(target=self._run_worker, daemon=True).start()

    def _run_worker(self) -> None:
        try:
            result = execute_plan(self.plan, self.mode_var.get())
            self.events.put(("run_done", result))
        except Exception as exc:
            self.events.put(("error", exc))

    def _render_plan(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in self.plan:
            self.tree.insert("", "end", values=(
                item.gallery_id or "",
                item.source.name,
                ", ".join(item.artists),
                item.title,
                str(item.destination) if item.destination else "",
                item.status,
            ))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "preview_done":
                    self.plan = payload
                    self._render_plan()
                    ready = sum(1 for x in self.plan if x.status == "준비")
                    matched = sum(1 for x in self.plan if x.gallery_id is not None and x.artists)
                    self._set_busy(False, f"총 {len(self.plan)}개 / 작가 매칭 {matched}개 / 실행 가능 {ready}개")
                elif kind == "download_done":
                    path = payload
                    self.db_var.set(str(path))
                    self.db_folder_var.set(str(Path(path).parent))
                    self._set_busy(False, "DB 갱신 완료")
                    messagebox.showinfo("완료", f"DB를 저장했습니다.\n{path}")
                elif kind == "run_done":
                    completed, skipped, errors = payload
                    self._set_busy(False, f"완료 {completed}개 / 건너뜀 {skipped}개 / 실패 {len(errors)}개")
                    detail = "\n".join(errors[:10])
                    if len(errors) > 10:
                        detail += f"\n... 외 {len(errors) - 10}개"
                    messagebox.showinfo(
                        "정리 완료",
                        f"완료: {completed}\n건너뜀: {skipped}\n실패: {len(errors)}"
                        + (f"\n\n{detail}" if detail else ""),
                    )
                    self.plan = []
                    self.run_btn.configure(state="disabled")
                elif kind == "error":
                    self._set_busy(False, "오류 발생")
                    messagebox.showerror("오류", str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _save(self) -> None:
        save_config({
            "source_dir": self.source_var.get(),
            "output_dir": self.output_var.get(),
            "db_path": self.db_var.get(),
            "db_folder": self.db_folder_var.get(),
            "db_url": self.url_var.get(),
            "mode": self.mode_var.get(),
            "artist_strategy": self.artist_strategy_var.get(),
        })

    def _on_close(self) -> None:
        self._save()
        self.destroy()


def run() -> None:
    app = ArtistSorterApp()
    app.mainloop()
