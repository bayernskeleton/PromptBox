# -*- mode: python ; coding: utf-8 -*-
# v2 2026-08-24 体积优化：
# 1. datas 不再打包整个 logos 目录，只带运行时实际使用的 3 个资源文件
#    （promptbox.ico / icon_256.png / logo_header_36.png），剔除 2.1M AI 原图冗余
# 2. excludes 排除 PIL 未使用的功能子模块（代码仅用 Image.open/resize + ImageTk.PhotoImage）
# 原版备份：PromptBox.spec.bak-20260824

a = Analysis(
    ['promptbox_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('promptbox_mvp', 'promptbox_mvp'),
        ('snippets.default.json', '.'),
        ('snippets.demo.json', '.'),
        # 保持目录形式与 v1 原版一致（图标加载行为零差异）。
        # 如需进一步瘦身：把 logos/ 下 3 张 AI 原图（Minimalist_*，共 2.1M）
        # 移出 logos/ 目录，此条目自动变小。
        ('logos', 'logos'),
    ],
    hiddenimports=['promptbox', 'docx', 'openpyxl', 'pptx', 'pypdf'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PIL.Image 内部有可选 numpy 数组接口支持，PyInstaller 会因此全量收集
        # numpy（约 20M+）；应用仅用 Image.open/resize + ImageTk，完全用不到
        'numpy',
        # PIL 未使用功能模块（仅保留 Image / ImageTk 及其核心依赖）
        'PIL.ImageDraw', 'PIL.ImageFont', 'PIL.ImageFilter', 'PIL.ImageGrab',
        'PIL.ImageEnhance', 'PIL.ImageOps', 'PIL.ImageChops', 'PIL.ImageColor',
        'PIL.ImageStat', 'PIL.ImageWin', 'PIL.ImageSequence', 'PIL.ImagePath',
        'PIL.ImageQt', 'PIL.ImageShow', 'PIL.Features',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PromptBox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logos\\promptbox.ico'],
)
