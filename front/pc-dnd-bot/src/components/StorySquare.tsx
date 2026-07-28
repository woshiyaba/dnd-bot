import { useEffect, useState } from 'react'
import { gameApi } from '../api/client'
import type { StorySummary } from '../types/game'

export function StorySquare({
  highlightCampaignId,
  onCreateStory,
  onJoinRoom,
  onSelectStory,
}: {
  highlightCampaignId?: string
  onCreateStory: () => void
  onJoinRoom: () => void
  onSelectStory: (story: StorySummary) => void
}) {
  const [stories, setStories] = useState<StorySummary[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    void gameApi
      .stories()
      .then(setStories)
      .catch((reason: unknown) =>
        setError(reason instanceof Error ? reason.message : '故事广场暂时无法开启'),
      )
      .finally(() => setIsLoading(false))
  }, [])

  return (
    <main className="story-square-screen">
      <div className="lobby-atmosphere" />
      <header className="square-header">
        <div className="square-brand">
          <div className="brand-rune">20</div>
          <div>
            <span>THE STORY ARCHIVE</span>
            <h1>故事广场</h1>
            <p>挑选一卷命运，召集同伴，让地下城主为你们揭开故事。</p>
          </div>
        </div>
        <div className="square-header-actions">
          <button className="text-button" onClick={onJoinRoom} type="button">
            输入房间码
          </button>
          <button className="primary-cta" onClick={onCreateStory} type="button">
            与 LLM 共创故事
          </button>
        </div>
      </header>

      <section className="square-content">
        <div className="square-section-title">
          <div>
            <small>CHOOSE YOUR FATE</small>
            <h2>可选剧本</h2>
          </div>
          <span>{stories.length} 卷已收录</span>
        </div>

        {isLoading ? <div className="story-empty">正在翻阅典藏……</div> : null}
        {error ? <div className="story-empty form-error">{error}</div> : null}
        {!isLoading && !error && stories.length === 0 ? (
          <div className="story-empty">广场尚无故事。写下第一卷冒险吧。</div>
        ) : null}

        <div className="story-card-grid">
          {stories.map((story) => (
            <article
              className={`story-card ${
                story.campaign_id === highlightCampaignId ? 'newly-published' : ''
              }`}
              key={story.campaign_id}
            >
              <div className="story-card-number">{String(story.beat_count).padStart(2, '0')}</div>
              {story.campaign_id === highlightCampaignId ? (
                <span className="published-badge">刚刚发布</span>
              ) : null}
              <small>{story.theme || '未命名主题'}</small>
              <h3>{story.title}</h3>
              <p>{story.premise}</p>
              <dl className="story-facts">
                <div><dt>时长</dt><dd>{story.duration_minutes} 分钟</dd></div>
                <div><dt>推荐</dt><dd>{story.recommended_player_count} 名玩家</dd></div>
                <div><dt>基调</dt><dd>{story.tone}</dd></div>
              </dl>
              <div className="story-tags">
                {story.gameplay_focus.map((tag) => <span key={tag}>{tag}</span>)}
              </div>
              {story.content_warnings.length ? (
                <p className="content-warning">
                  内容提示：{story.content_warnings.join('、')}
                </p>
              ) : null}
              <button
                className="primary-cta story-select"
                onClick={() => onSelectStory(story)}
                type="button"
              >
                选择此剧本
              </button>
            </article>
          ))}
        </div>
      </section>
    </main>
  )
}
