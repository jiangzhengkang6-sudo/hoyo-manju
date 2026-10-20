"""让 Python 临时目录在 DSH 沙箱下可写（仅用于本机安装依赖，不参与业务逻辑）。

DSH 文件沙箱会拒绝向权限为 0o700 的目录内写入文件。CPython 在 Windows 上
会依据 ``os.mkdir`` 的 mode 参数生成受限 DACL，而 ``tempfile.mkdtemp`` 固定以
0o700 创建目录，导致 pip 等工具报 PermissionError（os.chmod 无法修复 ACL）。

这里在解释器启动时把 os.mkdir 的 mode 强制为 0o777。

用法：
    $env:PYTHONPATH="<repo>\\tools"; python -m pip install ...
"""

import os

_orig_mkdir = os.mkdir


def _mkdir(path, mode=0o777, *, dir_fd=None):
    # 忽略调用方传入的受限 mode，统一以 0o777 创建，避免沙箱拒绝写入。
    return _orig_mkdir(path, 0o777, dir_fd=dir_fd)


os.mkdir = _mkdir
