import { useState } from 'react'
import './App.css'
import { DiceAnimator } from './components/DiceAnimator'
import { GameScreen } from './components/GameScreen'
import { LobbyScreen } from './components/LobbyScreen'
import { useGameRoom } from './hooks/useGameRoom'
import type { RoomAuthResponse, RoomCredential } from './types/game'

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
  }

  return credential ? (
    <AuthenticatedGame credential={credential} onLeave={leaveRoom} />
  ) : (
    <LobbyScreen onAuthenticated={authenticate} />
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
        lobby={game.lobby}
        onAction={game.submitAction}
        onFreeRoll={game.freeRoll}
        onInteractionRoll={game.rollInteraction}
        onLeave={onLeave}
        onLevelUp={game.submitLevelUp}
        onMessage={game.sendMessage}
        onStart={game.startRoom}
        session={game.session}
        streamText={game.streamText}
      />
      {game.rollAnimation ? (
        <DiceAnimator
          animation={game.rollAnimation}
          onComplete={game.dismissRoll}
        />
      ) : null}
    </>
  )
}

export default App
