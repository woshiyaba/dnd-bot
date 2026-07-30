import { useState } from 'react'
import './App.css'
import { DiceAnimator } from './components/DiceAnimator'
import { GameScreen } from './components/GameScreen'
import { LobbyScreen } from './components/LobbyScreen'
import { StorySquare } from './components/StorySquare'
import { StoryStudio } from './components/StoryStudio'
import { useGameRoom } from './hooks/useGameRoom'
import type { RoomAuthResponse, RoomCredential, StorySummary } from './types/game'

const STORAGE_KEY = 'dnd-bot-room-credential'

function readCredential(): RoomCredential | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value ? (JSON.parse(value) as RoomCredential) : null
  } catch {
    return null
  }
}

function App() {
  const [credential, setCredential] = useState<RoomCredential | null>(readCredential)
  const [view, setView] = useState<'square' | 'studio' | 'lobby'>('square')
  const [lobbyMode, setLobbyMode] = useState<'create' | 'join'>('create')
  const [selectedStory, setSelectedStory] = useState<StorySummary | null>(null)
  const [highlightCampaignId, setHighlightCampaignId] = useState<string>()

  function authenticate(response: RoomAuthResponse) {
    const next: RoomCredential = {
      roomCode: response.room.room_code,
      accessToken: response.access_token,
      member: response.member,
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    setCredential(next)
  }

  function leaveRoom() {
    localStorage.removeItem(STORAGE_KEY)
    setCredential(null)
    setView('square')
  }

  if (credential) {
    return <AuthenticatedGame credential={credential} onLeave={leaveRoom} />
  }
  if (view === 'studio') {
    return (
      <StoryStudio
        onBack={() => setView('square')}
        onPublished={(story) => {
          setHighlightCampaignId(story.campaign_id)
          setSelectedStory(story)
          setView('square')
        }}
      />
    )
  }
  if (view === 'lobby') {
    return (
      <LobbyScreen
        campaign={selectedStory}
        initialMode={lobbyMode}
        onAuthenticated={authenticate}
        onBack={() => setView('square')}
      />
    )
  }
  return (
    <StorySquare
      highlightCampaignId={highlightCampaignId}
      onCreateStory={() => setView('studio')}
      onJoinRoom={() => {
        setLobbyMode('join')
        setSelectedStory(null)
        setView('lobby')
      }}
      onSelectStory={(story) => {
        setSelectedStory(story)
        setLobbyMode('create')
        setView('lobby')
      }}
    />
  )
}

function AuthenticatedGame({
  credential,
  onLeave,
}: {
  credential: RoomCredential
  onLeave: () => void
}) {
  const game = useGameRoom(credential)
  return (
    <>
      <GameScreen
        credential={credential}
        error={game.error}
        isBusy={game.isBusy}
        isConnected={game.isConnected}
        isDmThinking={game.isDmThinking}
        lobby={game.lobby}
        onAction={game.submitAction}
        onFreeRoll={game.prepareFreeRoll}
        onLeave={onLeave}
        onLevelUp={game.submitLevelUp}
        onMessage={game.sendMessage}
        onStart={game.startRoom}
        session={game.session}
        streamText={game.streamText}
      />
      <DiceAnimator
        animation={game.rollAnimation}
        onRoll={game.startPreparedRoll}
        onComplete={game.dismissRoll}
      />
    </>
  )
}

export default App
