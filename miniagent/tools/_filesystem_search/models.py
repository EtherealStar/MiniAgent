from dataclasses import dataclass

@dataclass(frozen=True)
class SearchEntry:
    relative: str
    path: str
    is_directory: bool
