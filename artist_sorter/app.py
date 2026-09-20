from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .config import (
    config_path,
    last_operation_path,
    load_config,
    managed_db_path,
    save_config,
    undo_backup_dir,
)
from .core import (
    ExecutionResult,
    PlanItem,
    check_db_update,
    download_database,
    execute_plan,
    fetch_server_db_info,
    make_plan,
    normalize_db_download_url,
    plan_is_ready,
    plan_stats,
    undo_last_move,
    validate_violet_db,
)


DUPLICATE_LABEL_TO_VALUE = {
    "건너뛰기": "skip",
    "이름에 (1) 추가": "rename",
    "덮어쓰기": "overwrite",
}
DUPLICATE_VALUE_TO_LABEL = {
    value: label for label, value in DUPLICATE_LABEL_TO_VALUE.items()
}


class ArtistSorterApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Artist Sorter")
        self.geometry("1220x820")
        self.minsize(1020, 680)

        self.config_data = load_config()
        self.plan: list[PlanItem] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.sort_column: str | None = None
        self.sort_reverse = False
        self.column_labels: dict[str, str] = {}

        duplicate_value = self.config_data.get("duplicate_policy", "skip")
        duplicate_label = DUPLICATE_VALUE_TO_LABEL.get(
            duplicate_value,
            "건너뛰기",
        )

        self.source_var = tk.StringVar(
            value=self.config_data.get("source_dir", "")
        )
        self.output_var = tk.StringVar(
            value=self.config_data.get("output_dir", "")
        )
        self.db_var = tk.StringVar(
            value=self.config_data.get("db_path", str(managed_db_path()))
        )
        self.url_var = tk.StringVar(
            value=self.config_data.get("db_url", "")
        )
        self.mode_var = tk.StringVar(
            value=self.config_data.get("mode", "move")
        )
        self.artist_strategy_var = tk.StringVar(
            value=self.config_data.get("artist_strategy", "first")
        )
        self.recursive_var = tk.BooleanVar(
            value=bool(self.config_data.get("recursive", False))
        )
        self.duplicate_var = tk.StringVar(value=duplicate_label)
        self.db_status_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="준비됨")
        self.db_server_version = self._config_version()

        self._build_ui()
        self._refresh_undo_button()
        self.after(100, self._drain_events)
        self.after(500, self._start_db_check)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _config_version(self) -> int | None:
        value = self.config_data.get("db_server_version")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        self._path_row(
            outer, 0, "원본 폴더", self.source_var, self._pick_source
        )
        self._path_row(
            outer, 1, "정리 폴더", self.output_var, self._pick_output
        )
        self._path_row(
            outer, 2, "Violet DB", self.db_var, self._pick_db
        )

        ttk.Label(outer, text="Pi DB URL").grid(
            row=3, column=0, sticky="w", pady=4
        )
        ttk.Entry(outer, textvariable=self.url_var).grid(
            row=3,
            column=1,
            sticky="ew",
            padx=(8, 8),
            pady=4,
        )
        db_actions = ttk.Frame(outer)
        db_actions.grid(row=3, column=2, sticky="ew", pady=4)
        self.download_btn = ttk.Button(
            db_actions,
            text="DB 갱신",
            command=self._download_db,
        )
        self.download_btn.pack(side="left")
        ttk.Label(
            db_actions,
            textvariable=self.db_status_var,
        ).pack(side="left", padx=(8, 0))

        options = ttk.Frame(outer)
        options.grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(8, 4),
        )
        ttk.Label(options, text="실행 방식").pack(side="left")
        ttk.Radiobutton(
            options,
            text="이동",
            variable=self.mode_var,
            value="move",
        ).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(
            options,
            text="복사",
            variable=self.mode_var,
            value="copy",
        ).pack(side="left", padx=4)
        ttk.Separator(options, orient="vertical").pack(
            side="left", fill="y", padx=12
        )
        ttk.Label(options, text="작가가 여러 명일 때").pack(side="left")
        ttk.Radiobutton(
            options,
            text="첫 번째 작가",
            variable=self.artist_strategy_var,
            value="first",
            command=self._invalidate_plan,
        ).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(
            options,
            text="작가명 합치기",
            variable=self.artist_strategy_var,
            value="combined",
            command=self._invalidate_plan,
        ).pack(side="left", padx=4)

        options2 = ttk.Frame(outer)
        options2.grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, 8),
        )
        ttk.Checkbutton(
            options2,
            text="하위 폴더 포함",
            variable=self.recursive_var,
            command=self._invalidate_plan,
        ).pack(side="left")
        ttk.Separator(options2, orient="vertical").pack(
            side="left", fill="y", padx=12
        )
        ttk.Label(options2, text="중복 처리").pack(side="left")
        duplicate_box = ttk.Combobox(
            options2,
            textvariable=self.duplicate_var,
            values=list(DUPLICATE_LABEL_TO_VALUE),
            state="readonly",
            width=18,
        )
        duplicate_box.pack(side="left", padx=(8, 0))
        duplicate_box.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._invalidate_plan(),
        )

        actions = ttk.Frame(outer)
        actions.grid(
            row=6,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, 8),
        )
        self.preview_btn = ttk.Button(
            actions,
            text="1. 미리보기",
            command=self._preview,
        )
        self.preview_btn.pack(side="left")
        self.run_btn = ttk.Button(
            actions,
            text="2. 정리 실행",
            command=self._run_plan,
            state="disabled",
        )
        self.run_btn.pack(side="left", padx=8)
        self.undo_btn = ttk.Button(
            actions,
            text="마지막 이동 되돌리기",
            command=self._undo,
        )
        self.undo_btn.pack(side="left")
        ttk.Label(
            actions,
            textvariable=self.status_var,
        ).pack(side="right")

        table_frame = ttk.Frame(outer)
        table_frame.grid(
            row=7,
            column=0,
            columnspan=3,
            sticky="nsew",
        )
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        columns = (
            "id",
            "source",
            "artists",
            "title",
            "destination",
            "status",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
        )
        self.column_labels = {
            "id": "ID",
            "source": "원본",
            "artists": "작가",
            "title": "제목",
            "destination": "대상",
            "status": "상태",
        }
        widths = {
            "id": 90,
            "source": 200,
            "artists": 180,
            "title": 250,
            "destination": 300,
            "status": 130,
        }
        for col in columns:
            self.tree.heading(
                col,
                text=self.column_labels[col],
                command=lambda c=col: self._sort_tree(c),
            )
            self.tree.column(
                col,
                width=widths[col],
                minwidth=70,
                stretch=col in {
                    "source",
                    "artists",
                    "title",
                    "destination",
                },
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        yscroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        self.progress = ttk.Progressbar(
            outer,
            mode="indeterminate",
        )
        self.progress.grid(
            row=8,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(8, 0),
        )

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        var: tk.StringVar,
        command,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row,
            column=0,
            sticky="w",
            pady=4,
        )
        ttk.Entry(parent, textvariable=var).grid(
            row=row,
            column=1,
            sticky="ew",
            padx=(8, 8),
            pady=4,
        )
        ttk.Button(
            parent,
            text="찾아보기",
            command=command,
        ).grid(
            row=row,
            column=2,
            sticky="ew",
            pady=4,
        )

    def _pick_source(self) -> None:
        value = filedialog.askdirectory(
            title="히토미 다운로드 폴더 선택"
        )
        if value:
            self.source_var.set(value)
            if not self.output_var.get():
                self.output_var.set(
                    str(Path(value).parent / "sorted-by-artist")
                )
            self._invalidate_plan()

    def _pick_output(self) -> None:
        value = filedialog.askdirectory(
            title="정리 결과 폴더 선택"
        )
        if value:
            self.output_var.set(value)
            self._invalidate_plan()

    def _pick_db(self) -> None:
        value = filedialog.askopenfilename(
            title="Violet SQLite DB 선택",
            filetypes=[
                ("SQLite DB", "*.db"),
                ("모든 파일", "*.*"),
            ],
        )
        if not value:
            return
        try:
            validate_violet_db(Path(value))
        except Exception as exc:
            messagebox.showerror("DB 확인 실패", str(exc))
            return
        self.db_var.set(value)
        self._invalidate_plan()
        self._start_db_check()

    def _duplicate_policy(self) -> str:
        return DUPLICATE_LABEL_TO_VALUE.get(
            self.duplicate_var.get(),
            "skip",
        )

    def _scan_exclude_paths(self) -> list[Path]:
        paths = [
            config_path(),
            managed_db_path(),
            last_operation_path(),
            undo_backup_dir(),
        ]
        if getattr(sys, "frozen", False):
            paths.append(Path(sys.executable))
        return paths

    def _invalidate_plan(self) -> None:
        if self.plan:
            self.plan = []
            self.tree.delete(*self.tree.get_children())
            self.run_btn.configure(state="disabled")
            self.status_var.set("옵션 변경됨 - 미리보기를 다시 실행하세요.")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.preview_btn.configure(state=state)
        self.download_btn.configure(state=state)
        self.run_btn.configure(
            state=(
                "disabled"
                if busy or not self.plan
                else "normal"
            )
        )
        if busy:
            self.progress.start(10)
            if hasattr(self, "undo_btn"):
                self.undo_btn.configure(state="disabled")
        else:
            self.progress.stop()
            self._refresh_undo_button()
        if text:
            self.status_var.set(text)

    def _preview(self) -> None:
        try:
            source = Path(self.source_var.get())
            output_text = self.output_var.get().strip()
            if not output_text:
                raise ValueError("정리 폴더를 선택해주세요.")
            output = Path(output_text)
            db_path = Path(self.db_var.get())
            validate_violet_db(db_path)
            if not source.is_dir():
                raise FileNotFoundError(
                    "원본 폴더를 선택해주세요."
                )
        except Exception as exc:
            messagebox.showerror("확인 필요", str(exc))
            return

        strategy = self.artist_strategy_var.get()
        recursive = self.recursive_var.get()
        duplicate_policy = self._duplicate_policy()
        exclude_paths = self._scan_exclude_paths()

        self._set_busy(True, "스캔 중...")
        threading.Thread(
            target=self._preview_worker,
            args=(
                source,
                output,
                db_path,
                strategy,
                recursive,
                duplicate_policy,
                exclude_paths,
            ),
            daemon=True,
        ).start()

    def _preview_worker(
        self,
        source: Path,
        output: Path,
        db_path: Path,
        strategy: str,
        recursive: bool,
        duplicate_policy: str,
        exclude_paths: list[Path],
    ) -> None:
        try:
            plan = make_plan(
                source,
                output,
                db_path,
                strategy,
                recursive=recursive,
                duplicate_policy=duplicate_policy,
                exclude_paths=exclude_paths,
            )
            self.events.put(("preview_done", plan))
        except Exception as exc:
            self.events.put(("error", exc))

    def _start_db_check(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self.db_status_var.set("")
            return

        self.db_status_var.set("확인 중...")
        threading.Thread(
            target=self._db_check_worker,
            args=(
                url,
                Path(self.db_var.get()),
                self.db_server_version,
            ),
            daemon=True,
        ).start()

    def _db_check_worker(
        self,
        url: str,
        db_path: Path,
        known_version: int | None,
    ) -> None:
        try:
            available, info = check_db_update(
                url,
                db_path,
                known_version=known_version,
            )
            self.events.put(
                ("db_check_done", (available, info.version))
            )
        except Exception as exc:
            self.events.put(("db_check_error", str(exc)))

    def _download_db(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror(
                "URL 필요",
                "Pi DB URL을 입력해주세요.",
            )
            return

        self._set_busy(True, "DB 갱신 중...")
        threading.Thread(
            target=self._download_worker,
            args=(url, managed_db_path()),
            daemon=True,
        ).start()

    def _download_worker(
        self,
        url: str,
        destination: Path,
    ) -> None:
        version: int | None = None
        try:
            try:
                info = fetch_server_db_info(url)
                download_url = info.download_url
                version = info.version
            except Exception:
                download_url = normalize_db_download_url(url)

            path = download_database(
                download_url,
                destination,
            )
            self.events.put(
                ("download_done", (path, version))
            )
        except Exception as exc:
            self.events.put(("error", exc))

    def _run_plan(self) -> None:
        ready = sum(plan_is_ready(x) for x in self.plan)
        if ready == 0:
            messagebox.showinfo(
                "실행할 항목 없음",
                "미리보기 결과에서 실행 가능한 항목이 없습니다.",
            )
            return

        mode_label = (
            "이동" if self.mode_var.get() == "move" else "복사"
        )
        duplicate_label = self.duplicate_var.get()
        extra = ""
        if self._duplicate_policy() == "overwrite":
            extra = "\n중복 항목은 기존 대상을 덮어씁니다."

        if not messagebox.askyesno(
            "정리 실행",
            f"{ready}개 항목을 {mode_label}할까요?\n"
            f"중복 처리: {duplicate_label}{extra}",
        ):
            return

        mode = self.mode_var.get()
        self._set_busy(True, f"{mode_label} 중...")
        threading.Thread(
            target=self._run_worker,
            args=(mode,),
            daemon=True,
        ).start()

    def _run_worker(self, mode: str) -> None:
        try:
            result = execute_plan(
                self.plan,
                mode,
                undo_log_path=last_operation_path(),
                undo_backup_root=undo_backup_dir(),
            )
            self.events.put(("run_done", (result, mode)))
        except Exception as exc:
            self.events.put(("error", exc))

    def _undo(self) -> None:
        if not last_operation_path().is_file():
            self._refresh_undo_button()
            messagebox.showinfo(
                "되돌릴 작업 없음",
                "되돌릴 마지막 이동 작업이 없습니다.",
            )
            return

        if not messagebox.askyesno(
            "마지막 이동 되돌리기",
            "마지막 이동 작업을 원래 위치로 되돌릴까요?",
        ):
            return

        self._set_busy(True, "되돌리는 중...")
        threading.Thread(
            target=self._undo_worker,
            daemon=True,
        ).start()

    def _undo_worker(self) -> None:
        try:
            result = undo_last_move(
                last_operation_path(),
                undo_backup_dir(),
            )
            self.events.put(("undo_done", result))
        except Exception as exc:
            self.events.put(("error", exc))

    def _refresh_undo_button(self) -> None:
        state = (
            "normal"
            if last_operation_path().is_file()
            else "disabled"
        )
        if hasattr(self, "undo_btn"):
            self.undo_btn.configure(state=state)

    def _sort_tree(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._apply_tree_sort()

    def _apply_tree_sort(self) -> None:
        if not self.sort_column:
            return

        column = self.sort_column
        populated: list[tuple[object, str]] = []
        empty: list[str] = []

        for iid in self.tree.get_children(""):
            value = self.tree.set(iid, column)
            if value == "":
                empty.append(iid)
                continue
            if column == "id":
                try:
                    key: object = int(value)
                except ValueError:
                    key = value.casefold()
            else:
                key = value.casefold()
            populated.append((key, iid))

        populated.sort(
            key=lambda item: item[0],
            reverse=self.sort_reverse,
        )
        ordered = [iid for _, iid in populated] + empty
        for index, iid in enumerate(ordered):
            self.tree.move(iid, "", index)

        for col, label in self.column_labels.items():
            suffix = ""
            if col == column:
                suffix = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(
                col,
                text=label + suffix,
                command=lambda c=col: self._sort_tree(c),
            )

    def _render_plan(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in self.plan:
            if item.artists:
                artists = ", ".join(item.artists)
            elif item.groups:
                artists = "group: " + ", ".join(item.groups)
            elif item.artist_folder == "기타":
                artists = "N/A"
            else:
                artists = ""

            self.tree.insert(
                "",
                "end",
                values=(
                    item.gallery_id or "",
                    str(item.source),
                    artists,
                    item.title,
                    str(item.destination)
                    if item.destination
                    else "",
                    item.status,
                ),
            )
        self._apply_tree_sort()

    def _save(self) -> None:
        data = {
            "source_dir": self.source_var.get(),
            "output_dir": self.output_var.get(),
            "db_path": self.db_var.get(),
            "db_url": self.url_var.get(),
            "mode": self.mode_var.get(),
            "artist_strategy": self.artist_strategy_var.get(),
            "recursive": self.recursive_var.get(),
            "duplicate_policy": self._duplicate_policy(),
        }
        if self.db_server_version is not None:
            data["db_server_version"] = self.db_server_version
        save_config(data)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()

                if kind == "preview_done":
                    self.plan = payload
                    self._render_plan()
                    stats = plan_stats(self.plan)
                    self._set_busy(
                        False,
                        "작가 "
                        f"{stats['artists']}명 / "
                        f"그룹 {stats['groups']}개 / "
                        f"매칭 {stats['matched']}개 / "
                        f"N/A {stats['unknown_artist']}개 / "
                        f"DB 미매칭 {stats['db_unmatched']}개 / "
                        f"실행 가능 {stats['ready']}개",
                    )

                elif kind == "db_check_done":
                    available, version = payload
                    if available:
                        self.db_status_var.set("새 DB 있음")
                        self.download_btn.configure(
                            text="DB 갱신 (새 DB 있음)"
                        )
                    else:
                        self.db_status_var.set("DB 최신")
                        self.download_btn.configure(text="DB 갱신")

                elif kind == "db_check_error":
                    self.db_status_var.set("확인 실패")
                    self.download_btn.configure(text="DB 갱신")

                elif kind == "download_done":
                    path, version = payload
                    self.db_var.set(str(path))
                    if version is not None:
                        self.db_server_version = int(version)
                    self._save()
                    self.db_status_var.set("DB 최신")
                    self.download_btn.configure(text="DB 갱신")
                    self._invalidate_plan()
                    self._set_busy(False, "DB 갱신 완료")
                    messagebox.showinfo(
                        "완료",
                        f"DB를 갱신했습니다.\n{path}",
                    )

                elif kind == "run_done":
                    result, mode = payload
                    assert isinstance(result, ExecutionResult)
                    self._set_busy(
                        False,
                        f"완료 {result.completed}개 / "
                        f"건너뜀 {result.skipped}개 / "
                        f"실패 {len(result.errors)}개",
                    )
                    self._refresh_undo_button()
                    detail = "\n".join(result.errors[:10])
                    if len(result.errors) > 10:
                        detail += (
                            f"\n... 외 "
                            f"{len(result.errors) - 10}개"
                        )
                    messagebox.showinfo(
                        "정리 완료",
                        f"완료: {result.completed}\n"
                        f"건너뜀: {result.skipped}\n"
                        f"실패: {len(result.errors)}"
                        + (f"\n\n{detail}" if detail else ""),
                    )
                    self.plan = []
                    self.tree.delete(*self.tree.get_children())
                    self.run_btn.configure(state="disabled")

                elif kind == "undo_done":
                    result = payload
                    assert isinstance(result, ExecutionResult)
                    self._set_busy(
                        False,
                        f"되돌림 {result.completed}개 / "
                        f"실패 {len(result.errors)}개",
                    )
                    self._refresh_undo_button()
                    self.plan = []
                    self.tree.delete(*self.tree.get_children())
                    detail = "\n".join(result.errors[:10])
                    messagebox.showinfo(
                        "되돌리기 완료",
                        f"되돌림: {result.completed}\n"
                        f"실패: {len(result.errors)}"
                        + (f"\n\n{detail}" if detail else ""),
                    )

                elif kind == "error":
                    self._set_busy(False, "오류 발생")
                    self._refresh_undo_button()
                    messagebox.showerror("오류", str(payload))
        except queue.Empty:
            pass

        self.after(100, self._drain_events)

    def _on_close(self) -> None:
        self._save()
        self.destroy()


def run() -> None:
    app = ArtistSorterApp()
    app.mainloop()
