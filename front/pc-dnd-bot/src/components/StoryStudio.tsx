import { useEffect, useState } from 'react'
import { ApiError, gameApi } from '../api/client'
import type {
  StoryConversationMessage,
  StoryDraftResponse,
  StoryGenerationTaskResponse,
  StoryInterviewResponse,
  StorySummary,
} from '../types/game'

const STORAGE_KEY = 'dnd-bot-story-workbench'

type StudioSnapshot = {
  conversation: StoryConversationMessage[]
  designBrief: Record<string, unknown>
  interview: StoryInterviewResponse | null
  draft: StoryDraftResponse | null
  taskId: string | null
}

const EMPTY_SNAPSHOT: StudioSnapshot = {
  conversation: [],
  designBrief: {},
  interview: null,
  draft: null,
  taskId: null,
}

function readSnapshot(): StudioSnapshot {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return EMPTY_SNAPSHOT
    const value = JSON.parse(raw) as Partial<StudioSnapshot>
    return {
      conversation: Array.isArray(value.conversation) ? value.conversation : [],
      designBrief: value.designBrief ?? {},
      interview: value.interview ?? null,
      draft: value.draft ?? null,
      taskId: typeof value.taskId === 'string' ? value.taskId : null,
    }
  } catch {
    return EMPTY_SNAPSHOT
  }
}

export function StoryStudio({
  onBack,
  onPublished,
}: {
  onBack: () => void
  onPublished: (story: StorySummary) => void
}) {
  const restored = readSnapshot()
  const [conversation, setConversation] = useState(restored.conversation)
  const [designBrief, setDesignBrief] = useState(restored.designBrief)
  const [interview, setInterview] = useState(restored.interview)
  const [draft, setDraft] = useState(restored.draft)
  const [taskId, setTaskId] = useState(restored.taskId)
  const [generationTask, setGenerationTask] = useState<StoryGenerationTaskResponse | null>(null)
  const [input, setInput] = useState('')
  const [isBusy, setIsBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ conversation, designBrief, interview, draft, taskId }),
    )
  }, [conversation, designBrief, draft, interview, taskId])

  useEffect(() => {
    if (!taskId) return
    let stopped = false
    let timer: number | undefined
    const refresh = async () => {
      try {
        const next = await gameApi.storyGenerationTask(taskId)
        if (stopped) return
        setGenerationTask(next)
        if (next.draft) {
          setDraft(next.draft)
        } else if (next.status === 'completed') {
          setDraft(null)
          setError('已完成任务的草稿已过期，请重新生成')
        }
        if (next.status === 'failed') setError(next.error ?? '剧本生成失败')
        if (next.status === 'cancelled') setError('故事生成已取消')
        if (['completed', 'failed', 'cancelled'].includes(next.status) && timer) {
          window.clearInterval(timer)
        }
      } catch (reason) {
        if (stopped) return
        if (reason instanceof ApiError && reason.status === 404) {
          if (timer) window.clearInterval(timer)
          setTaskId(null)
          setGenerationTask(null)
          setDraft(null)
          setError('故事生成任务和草稿已过期，请重新生成')
        } else {
          setError(reason instanceof Error ? reason.message : '无法恢复故事生成任务')
        }
      }
    }
    void refresh()
    timer = window.setInterval(() => void refresh(), 2_000)
    return () => {
      stopped = true
      if (timer) window.clearInterval(timer)
    }
  }, [taskId])

  const isGenerating = Boolean(
    generationTask && ['queued', 'running', 'cancel_requested'].includes(generationTask.status),
  )

  function acceptInterview(
    response: StoryInterviewResponse,
    baseConversation: StoryConversationMessage[],
  ) {
    setConversation([
      ...baseConversation,
      { role: 'assistant', content: response.assistant_message },
    ])
    setDesignBrief(response.design_brief)
    setInterview(response)
  }

  async function sendMessage(event: React.FormEvent) {
    event.preventDefault()
    const content = input.trim()
    if (!content || isBusy || draft || isGenerating) return
    const nextConversation: StoryConversationMessage[] = [
      ...conversation,
      { role: 'user', content },
    ]
    setConversation(nextConversation)
    setInput('')
    setIsBusy(true)
    setError('')
    try {
      const response = await gameApi.interviewStory(nextConversation, designBrief)
      acceptInterview(response, nextConversation)
    } catch (reason) {
      setConversation(conversation)
      setInput(content)
      setError(reason instanceof Error ? reason.message : '故事策划没有成功回应')
    } finally {
      setIsBusy(false)
    }
  }

  async function confirmAndGenerate() {
    if (!interview || interview.status !== 'ready_for_confirmation' || isBusy) return
    const confirmation: StoryConversationMessage = {
      role: 'user',
      content: '确认，按这份最终设计生成剧本。',
    }
    const nextConversation = [...conversation, confirmation]
    setConversation(nextConversation)
    setIsBusy(true)
    setError('')
    let confirmationResponded = false
    try {
      const confirmed = await gameApi.interviewStory(nextConversation, designBrief)
      confirmationResponded = true
      acceptInterview(confirmed, nextConversation)
      if (confirmed.status !== 'confirmed') {
        throw new Error('故事设计尚未得到有效确认，请根据策划提示继续沟通')
      }
      const task = await gameApi.createStoryGenerationTask(confirmed.design_brief)
      setTaskId(task.task_id)
      setGenerationTask(task)
      setDraft(null)
    } catch (reason) {
      if (!confirmationResponded) setConversation(conversation)
      setError(reason instanceof Error ? reason.message : '剧本生成失败')
    } finally {
      setIsBusy(false)
    }
  }

  async function regenerateDraft() {
    if (isBusy || isGenerating) return
    setIsBusy(true)
    setError('')
    try {
      const task = await gameApi.createStoryGenerationTask(designBrief)
      setTaskId(task.task_id)
      setGenerationTask(task)
      setDraft(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '剧本重新生成失败')
    } finally {
      setIsBusy(false)
    }
  }

  async function publish() {
    if (!draft || isBusy) return
    setIsBusy(true)
    setError('')
    try {
      const response = await gameApi.publishStory(draft.draft_id)
      localStorage.removeItem(STORAGE_KEY)
      onPublished(response.story)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '剧本发布失败')
    } finally {
      setIsBusy(false)
    }
  }

  async function cancelGeneration() {
    if (!taskId || !isGenerating) return
    setError('')
    try {
      setGenerationTask(await gameApi.cancelStoryGenerationTask(taskId))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '取消生成失败')
    }
  }

  function reset() {
    localStorage.removeItem(STORAGE_KEY)
    setConversation([])
    setDesignBrief({})
    setInterview(null)
    setDraft(null)
    setTaskId(null)
    setGenerationTask(null)
    setInput('')
    setError('')
  }

  function chooseOption(option: string) {
    setInput((current) => current.trim() ? `${current.trim()}；${option}` : option)
  }

  return (
    <main className="story-studio-screen">
      <div className="lobby-atmosphere" />
      <header className="studio-header">
        <button className="text-button" onClick={onBack} type="button">← 返回广场</button>
        <div>
          <small>STORY FORGE</small>
          <h1>故事熔炉</h1>
          <p>告诉策划你想经历怎样的冒险。方向确认后，编剧会把它铸成可运行的剧本。</p>
        </div>
        <button className="text-button" onClick={reset} type="button">新故事</button>
      </header>

      <section className="studio-layout">
        <div className="studio-chat">
          <div className="studio-messages">
            {conversation.length === 0 ? (
              <div className="studio-welcome">
                <span>✦</span>
                <h2>从一个念头开始</h2>
                <p>比如：“我想玩一场发生在漂浮城市、以调查和社交为主的冒险。”</p>
              </div>
            ) : null}
            {conversation.map((message, index) => (
              <article className={`studio-message ${message.role}`} key={`${message.role}-${index}`}>
                <small>{message.role === 'user' ? '你' : '故事策划'}</small>
                <p>{message.content}</p>
              </article>
            ))}
            {isBusy ? <div className="studio-thinking">命运的墨迹正在汇聚……</div> : null}
            {generationTask && !draft ? (
              <div className="studio-generation-progress" aria-live="polite">
                <div>
                  <strong>{generationTask.stage}</strong>
                  <span>{generationTask.progress}%</span>
                </div>
                <progress max={100} value={generationTask.progress} />
                {isGenerating ? (
                  <button className="text-button" onClick={() => void cancelGeneration()} type="button">
                    取消生成
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>

          {!draft ? (
            <form className="studio-composer" onSubmit={sendMessage}>
              {interview?.questions.map((question) => (
                <div className="studio-question" key={question.id}>
                  <strong>{question.question}</strong>
                  {question.why_it_matters ? <small>{question.why_it_matters}</small> : null}
                  <div>
                    {question.suggested_options.map((option) => (
                      <button onClick={() => chooseOption(option)} type="button" key={option}>
                        {option}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
              <div className="studio-input-row">
                <textarea
                  disabled={isBusy || isGenerating}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder={
                    interview?.status === 'ready_for_confirmation'
                      ? '如果需要修改，请在这里告诉策划……'
                      : '描述你的构思，或回答策划的问题……'
                  }
                  rows={3}
                  value={input}
                />
                <button className="primary-cta" disabled={isBusy || !input.trim()} type="submit">
                  发送
                </button>
              </div>
              {interview?.status === 'ready_for_confirmation' ? (
                <button
                  className="primary-cta confirm-story"
                  disabled={isBusy || isGenerating}
                  onClick={() => void confirmAndGenerate()}
                  type="button"
                >
                  确认设计并生成剧本
                </button>
              ) : null}
              {interview?.status === 'confirmed' ? (
                <button
                  className="primary-cta confirm-story"
                  disabled={isBusy}
                  onClick={() => void regenerateDraft()}
                  type="button"
                >
                  重新生成剧本预览
                </button>
              ) : null}
            </form>
          ) : null}
          {error ? <p className="form-error studio-error">{error}</p> : null}
        </div>

        <aside className="studio-sidebar">
          <small>DESIGN BRIEF</small>
          <h2>{String(designBrief.working_title ?? '尚未命名')}</h2>
          <p>{String(designBrief.premise ?? '与策划开始沟通后，这里会逐步形成你的故事设计稿。')}</p>
          <dl>
            <div><dt>玩家身份</dt><dd>{String(designBrief.player_role ?? '待确认')}</dd></div>
            <div><dt>核心冲突</dt><dd>{String(designBrief.core_conflict ?? '待确认')}</dd></div>
            <div><dt>基调</dt><dd>{String(designBrief.tone ?? '待确认')}</dd></div>
            <div><dt>预计时长</dt><dd>{designBrief.duration_minutes ? `${String(designBrief.duration_minutes)} 分钟` : '待确认'}</dd></div>
            <div><dt>推荐人数</dt><dd>{designBrief.player_count ? `${String(designBrief.player_count)} 人` : '待确认'}</dd></div>
          </dl>

          {draft ? (
            <div className="story-preview-card">
              <span>CANON READY</span>
              <h3>{draft.story.title}</h3>
              <p>{draft.story.premise}</p>
              <div className="story-tags">
                {draft.story.gameplay_focus.map((tag) => <span key={tag}>{tag}</span>)}
              </div>
              <small>预览将在 {new Date(draft.expires_at).toLocaleTimeString()} 失效</small>
              {draft.quality ? (
                <dl className="story-quality-grid">
                  <div><dt>结构</dt><dd>{draft.quality.act_count} Act · {draft.quality.playable_beat_count} Beat</dd></div>
                  <div><dt>内容</dt><dd>{draft.quality.location_count} 地点 · {draft.quality.clue_count} 线索</dd></div>
                  <div><dt>流程</dt><dd>{draft.quality.encounter_count} 遭遇 · {draft.quality.branch_count} 分支</dd></div>
                  <div><dt>路径</dt><dd>{draft.quality.shortest_minutes}–{draft.quality.longest_minutes} 分钟</dd></div>
                  <div><dt>质量</dt><dd>{draft.quality.continuity_passed ? '连贯性复核通过' : '兼容草稿'} · 修复 {draft.quality.repair_count} 次</dd></div>
                </dl>
              ) : null}
              <button className="primary-cta" disabled={isBusy} onClick={() => void publish()} type="button">
                {isBusy ? '正在发布……' : '发布到故事广场'}
              </button>
              <button className="text-button" disabled={isBusy} onClick={() => void regenerateDraft()} type="button">
                重新生成
              </button>
            </div>
          ) : null}
        </aside>
      </section>
    </main>
  )
}
