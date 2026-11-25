"""Input / output models of the search tool."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    type: str = Field(description="種別（issue, wiki, news, version, project, file, attachment, time_entry）")
    id: str = Field(description="種別ごとの ID。wiki は「プロジェクト識別子:ページ名」。本文取得にはこの id を使う")
    project_id: int
    title: str
    excerpt: str = Field(description="該当チャンクの抜粋（本文全体ではない）")
    score: float = Field(description="類似度（コサイン、大きいほど近い）")
    url: str
    chunk_no: int


class SearchResponse(BaseModel):
    results: list[SearchResult]
    count: int
    incomplete: bool = Field(description="true のとき、見えるはずの結果が欠けている可能性がある")
    unverified_count: int = Field(description="権限を確認できず除外した件数")
    notice: str | None = None
