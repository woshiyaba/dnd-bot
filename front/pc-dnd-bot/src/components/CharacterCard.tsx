import type { CharacterView } from '../types/game'

export function CharacterAvatar({
  name,
  color,
  size = 'medium',
}: {
  name: string
  color: string
  size?: 'small' | 'medium' | 'large'
}) {
  return (
    <div
      className={`character-avatar avatar-${size}`}
      style={{ '--character-color': color } as React.CSSProperties}
      aria-hidden="true"
    >
      {name.slice(0, 1)}
    </div>
  )
}

export function CharacterCard({ character }: { character: CharacterView }) {
  const hpPercent = Math.max(
    0,
    Math.min(100, (character.current_hp / Math.max(character.max_hp, 1)) * 100),
  )
  const isDown = character.current_hp <= 0
  return (
    <article
      className={`character-card ${isDown ? 'is-down' : ''}`}
      style={{ '--character-color': character.color } as React.CSSProperties}
    >
      <div className="character-card-top">
        <CharacterAvatar name={character.name} color={character.color} />
        <div className="character-identity">
          <div>
            <strong>{character.name}</strong>
            <i className={character.is_online ? 'online' : ''} />
          </div>
          <span>
            Lv.{character.level} {character.char_class ?? '冒险者'}
          </span>
          {character.display_name ? <small>{character.display_name}</small> : null}
        </div>
      </div>
      <div className="character-stats">
        <span>♥ {character.current_hp}/{character.max_hp}</span>
        <span>◇ AC {character.ac}</span>
      </div>
      <div className="hp-track">
        <span style={{ width: `${hpPercent}%` }} />
      </div>
      {character.conditions.length ? (
        <div className="condition-list">
          {character.conditions.map((condition) => (
            <span key={condition}>{condition}</span>
          ))}
        </div>
      ) : null}
    </article>
  )
}
