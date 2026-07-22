class ProviderError(Exception):
    """供应商调用前错误，不属于响应流终态。"""


class ProviderNotConfiguredError(ProviderError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__("模型供应商未配置: " + ", ".join(missing))


class ProviderConfigurationError(ProviderError):
    pass


class ModelContractError(ProviderError):
    pass
