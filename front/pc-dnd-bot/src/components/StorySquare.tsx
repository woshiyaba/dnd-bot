import { useEffect, useState } from 'react'
import { gameApi } from '../api/client'
import type { StorySummary } from '../types/game'

export function StorySquare({
  highlightCampaignId,
  onCreateStory,
  onJoinRoom,
  onSelectStory,
  onResumeRoom,
}: {
  highlightCampaignId?: string
  onCreateStory: () => void
  onJoinRoom: () => void
  onSelectStory: (story: StorySummary) => void
  onResumeRoom?: () => void
}) {
  const [stories, setStories] = useState<StorySummary[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const [copiedId, setCopiedId] = useState('')
  const sharedId = new URLSearchParams(window.location.search).get('campaign')
  const sharedStory = stories.find((story) => story.campaign_id === sharedId)
  const visibleStories = stories.filter((story) =>
    `${story.title} ${story.premise} ${story.gameplay_focus.join(' ')}`.toLowerCase().includes(query.trim().toLowerCase()),
  )

  async function share(story: StorySummary) {
    const url = new URL(window.location.href)
    url.search = ''
    url.hash = ''
    url.searchParams.set('campaign', story.campaign_id)
    try {
      await navigator.clipboard.writeText(url.toString())
      setCopiedId(story.campaign_id)
      setError('')
    } catch {
      setError('无法自动复制，请从下面的链接打开剧本，再复制浏览器地址。')
    }
  }

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
          {onResumeRoom ? <button className="text-button" onClick={onResumeRoom} type="button">继续当前冒险</button> : null}
          <button className="text-button" onClick={onJoinRoom} type="button">
            输入房间码
          </button>
          <button className="primary-cta" onClick={onCreateStory} type="button">
            创作并发布剧本
          </button>
        </div>
      </header>

      <section className="square-content">
        {sharedStory ? (
          <div className="shared-story-banner">
            <div><small>同伴分享的剧本</small><h2>{sharedStory.title}</h2><p>{sharedStory.premise}</p></div>
            <button className="primary-cta" onClick={() => onSelectStory(sharedStory)} type="button">用此剧本开团</button>
          </div>
        ) : sharedId && !isLoading && !error ? <p role="status">分享的剧本尚未发布或在当前服务器不存在，请从下方选择。</p> : null}
        <div className="square-section-title">
          <div>
            <small>CHOOSE YOUR FATE</small>
            <h2>可选剧本</h2>
          </div>
          <span>{stories.length} 卷已收录</span>
        </div>
        <label className="story-search">寻找冒险
          <input type="search" placeholder="搜索剧本名称、简介或玩法" value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>

        {isLoading ? <div className="story-empty">正在翻阅典藏……</div> : null}
        {error ? <div className="story-empty form-error">{error}</div> : null}
        {!isLoading && !error && stories.length === 0 ? (
          <div className="story-empty">广场尚无故事。写下第一卷冒险吧。</div>
        ) : null}

        <div className="story-card-grid">
          {visibleStories.map((story) => (
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
              <div className="story-share">
                <button className="text-button" onClick={() => void share(story)} type="button">
                  {copiedId === story.campaign_id ? '链接已复制' : '复制分享链接'}
                </button>
                <a href={`?campaign=${encodeURIComponent(story.campaign_id)}`}>剧本链接</a>
              </div>
            </article>
          ))}
        </div>
        {!isLoading && stories.length > 0 && visibleStories.length === 0 ? <p className="story-empty">没有匹配的剧本，试试其他关键词。</p> : null}
      </section>
    </main>
  )
}
