"""验收：模板包导出再导入，产物与指针都不丢（第 138 轮，§19.36）。

    .\\\\.venv\\\\Scripts\\\\python.exe scripts\\\\verify-r138-template-package.py

做四件事，每件都对着一个具体的坏法：

1. **导出**（走真端点 `GET /video/template/package`）。
2. **导入进一个临时项目**（走真端点同一条服务路径），逐字节比 22 张图。
   坏法：包里少一张、PNG 被重编码、`avatar3_mask.png` 被写成 `avatar2_mask.png`。
3. **在导入的项目上重新导出**，与第一次的包逐成员比哈希。
   坏法：manifest 里的 rect 在导入时被丢掉——那时图是对的，位置是错的。
4. **检查指针**：`poster_of` / `art_of` / `box_source` 在新项目上必须指向新目录里**存在**的文件。
   坏法：`background_path` 是其他所有路径的锚点，锚点错了文件仍在磁盘上，只是没人读。
   这一条是「渲染能不能用」的可判据版本；画面本身的逐像素等价由第 137 轮
   （`docs/video.md` §19.35.1，`render_still` 新旧比 PSNR `inf`）与第 138 轮的
   编译产物验收钉住，这里不重复跑一分钟的编码。

判据是**零差异**，不是「看起来一样」：模板包最容易坏的方式就是不报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_PROJECT = "prj_1m6v2m8anfpyqq3w8qt"

#: Every image the package must carry. Four background twins, the two art layers, and
#: four avatars times four files.
SHARED = [
    "background.png",
    "background_bare.png",
    "background_open.png",
    "background_bare_open.png",
    "plate.png",
    "overlay.png",
] + [f"avatar{index}_{kind}.png" for index in range(4) for kind in ("lit", "dim", "frame", "mask")]


def _project_path() -> None:
    """Make ``animatter`` importable **after** ``sys.path`` is set up."""
    src = str(ROOT / "apps" / "api" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def main() -> int:
    _project_path()

    from animatter.config import Settings
    from animatter.db import Database
    from animatter.media import fonts
    from animatter.services import projects as project_service
    from animatter.services import video_template_package as package_service
    from animatter.services import video_templates as template_service

    settings = Settings()
    database = Database(settings)
    failures: list[str] = []

    with database.session() as session:
        source = template_service.require(session, SOURCE_PROJECT)
        name, payload = package_service.export_package(session, settings, SOURCE_PROJECT)
        print(f"1. exported  {name}  {len(payload):,} B")

        scratch = project_service.create_project(
            session, settings, "模板包验收（临时）", with_template=False
        )
        session.commit()
        try:
            installed = package_service.import_package(session, settings, scratch.id, payload)
            session.commit()
            print(f"2. installed vtpl={installed.id}  project={scratch.id}")

            # --- 1. every image, byte for byte ---------------------------------
            source_build = Path(source.background_path).parent
            target_build = Path(installed.background_path).parent
            missing = [f for f in SHARED if not (target_build / f).is_file()]
            differing = [
                f
                for f in SHARED
                if (target_build / f).is_file()
                and (source_build / f).read_bytes() != (target_build / f).read_bytes()
            ]
            print(f"3. compared {len(SHARED)} images  missing={len(missing)}  differing={len(differing)}")
            failures += [f"没有搬过来: {f}" for f in missing]
            failures += [f"字节不同: {f}" for f in differing]

            # --- 2. re-export from the imported project ------------------------
            _, again = package_service.export_package(session, settings, scratch.id)
            first, second = _members(payload), _members(again)
            # manifest.json carries ``created_at`` and the project-derived name, so its bytes
            # legitimately move. Every *image* and font must not.
            moving = {"manifest.json"}
            lost = sorted((set(first) - set(second)) - moving)
            changed = sorted(
                name
                for name in set(first) & set(second) - moving
                if first[name] != second[name]
            )
            print(f"4. re-exported  members={len(second)}  lost={len(lost)}  changed={len(changed)}")
            failures += [f"重导出丢成员: {name}" for name in lost]
            failures += [f"重导出内容变了: {name}" for name in changed]

            # --- 3. the pointers a render actually follows ---------------------
            poster = template_service.poster_of(installed, bare=False)
            bare = template_service.poster_of(installed, bare=True)
            plate, stroke = template_service.art_of(installed)
            box_path, origin = template_service.box_source(
                installed, template_service.box_rect_of(installed)
            )
            pointers = {
                "poster": poster,
                "poster_bare": bare,
                "plate": plate.path if plate else None,
                "stroke": stroke.path if stroke else None,
                "box": box_path,
            }
            for label, path in pointers.items():
                if path is None:
                    failures.append(f"指针缺失: {label}")
                elif not Path(path).is_file():
                    failures.append(f"指针指向不存在的文件: {label} -> {path}")
                elif Path(path).parent != target_build:
                    failures.append(f"指针没指向新目录: {label} -> {path}")
            print(f"5. pointers  {', '.join(f'{k}={Path(v).name}' if v else f'{k}=None' for k, v in pointers.items())}")
            print(f"   box origin {origin}")

            # --- 4. the fonts the package brought -----------------------------
            fonts.set_private_dir(settings.fonts_dir)
            for family in ("Baigo", "Alimama ShuHeiTi"):
                if fonts.resolve(family) is None:
                    failures.append(f"字体没有装上: {family}")
            print(f"6. fonts     {', '.join(sorted(p.name for p in Path(settings.fonts_dir).glob('*')))}")
        finally:
            _drop(settings, scratch.id)

    if failures:
        print("\\nFAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\\nOK — 包导出再导入后 22 张图逐字节相同、指针全部落在新目录、字体可用")
    return 0


def _members(payload: bytes) -> dict[str, bytes]:
    """The zip's members, so two packages can be compared member by member."""
    import zipfile

    with zipfile.ZipFile(_buffer(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _buffer(payload: bytes):  # noqa: ANN201 - io.BytesIO, imported lazily to keep the top clean
    import io

    return io.BytesIO(payload)


def _drop(settings, project_id: str) -> None:
    """Remove the scratch project and its directory, whatever happened above."""
    import shutil

    from animatter.db import Database
    from animatter.models import Project

    with Database(settings).session() as session:
        project = session.get(Project, project_id)
        if project is not None:
            session.delete(project)
            session.commit()
    shutil.rmtree(settings.projects_dir / project_id, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
