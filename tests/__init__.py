"""测试包入口。

导入时把代理环境固定为「无代理」，使整套测试与开发机的 ``HTTPS_PROXY`` /
``HTTP_PROXY`` / ``ALL_PROXY`` 设置解耦 —— 详见 :mod:`tests.proxy_env_isolation`。
需要验证代理路径的用例自行显式设置并还原环境变量。
"""

from .proxy_env_isolation import clear_proxy_environment

clear_proxy_environment()
