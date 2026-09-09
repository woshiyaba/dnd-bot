import { useState } from 'react'
import type { CharacterView, SessionView } from '../types/game'

export function ExplorationPanel({ session, disabled, onMessage }: {
  session: SessionView
  disabled: boolean
  onMessage: (content: string) => Promise<void>
}) {
  return (
    <div className="exploration-panel">
      <p>描述意图，由地下城主判断是否需要检定。你也可以在聊天中提出任何其他行动。</p>
      <div className="action-grid">
        {['观察周围环境', '仔细调查现场', '倾听附近动静'].map((intent) => (
          <button key={intent} disabled={disabled} onClick={() => void onMessage(`我想${intent}。`)} type="button">
            <span>⌕</span><strong>{intent}</strong>
          </button>
        ))}
        {session.scene.exits.map((exit) => (
          <button key={exit} disabled={disabled} onClick={() => void onMessage(`我提议队伍前往${exit}，先观察沿途是否安全。`)} type="button">
            <span>➤</span><strong>{exit}</strong><small>提出前往此处</small>
          </button>
        ))}
        {session.scene.actors.map((actor) => (
          <button key={actor.id} disabled={disabled} onClick={() => void onMessage(`我尝试与${actor.name}交谈。`)} type="button">
            <span>♧</span><strong>{actor.name}</strong><small>{actor.disposition === 'hostile' ? '尝试交涉 · 对方有敌意' : '与其交谈'}</small>
          </button>
        ))}
      </div>
      {session.scene.visited_locations.length > 0 ? (
        <p className="travel-history">走过的地方：{session.scene.visited_locations.join(' → ')}</p>
      ) : null}
    </div>
  )
}

export function InventoryPanel({ me, party, disabled, onTransferItem }: {
  me: CharacterView
  party: CharacterView[]
  disabled: boolean
  onTransferItem: (itemId: string, targetId: string, quantity: number) => Promise<void>
}) {
  const [itemId, setItemId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [quantity, setQuantity] = useState(1)
  const selectedItem = me.inventory.find((item) => item.item_id === itemId)
  const recipients = party.filter((actor) => actor.id !== me.id && actor.current_hp > 0)
  return (
    <div className="inventory-panel">
      <div className="inventory-grid">
        {me.inventory.map((item) => (
          <article key={item.item_id}>
            <strong>{item.name}</strong><span>× {item.quantity}</span>
          </article>
        ))}
        {me.inventory.length === 0 ? <p>背包为空。探索获得的物品会记录在这里。</p> : null}
      </div>
      {me.equipment.length > 0 ? <p>随身装备：{me.equipment.join('、')}</p> : null}
      {recipients.length > 0 && me.inventory.length > 0 ? (
        <form className="inventory-transfer" onSubmit={(event) => {
          event.preventDefault()
          if (!disabled && selectedItem && quantity > 0 && quantity <= selectedItem.quantity && recipients.some((actor) => actor.id === targetId)) {
            void onTransferItem(itemId, targetId, quantity)
          }
        }}>
          <label>物品<select value={itemId} onChange={(event) => { setItemId(event.target.value); setQuantity(1) }} required>
            <option value="">选择物品</option>
            {me.inventory.map((item) => <option key={item.item_id} value={item.item_id}>{item.name} ×{item.quantity}</option>)}
          </select></label>
          <label>交给<select value={targetId} onChange={(event) => setTargetId(event.target.value)} required>
            <option value="">选择队友</option>
            {recipients.map((actor) => <option key={actor.id} value={actor.id}>{actor.name}</option>)}
          </select></label>
          <label>数量<input type="number" min={1} max={selectedItem?.quantity ?? 1} step={1} value={quantity} onChange={(event) => setQuantity(event.target.valueAsNumber)} required /></label>
          <button type="submit" disabled={disabled || !selectedItem || !targetId}>转交物品</button>
          <small>交接物品需要处于探索阶段，并先完成当前交互。</small>
        </form>
      ) : null}
    </div>
  )
}
