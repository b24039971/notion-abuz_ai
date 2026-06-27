import React, { useEffect, useState } from 'react'

export interface ModelMapResp {
  model_map: Record<string, string>
  available_models: { id: string, name: string }[]
}

export function AdminModels() {
  const [data, setData] = useState<ModelMapResp | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetch('/admin/models')
      .then(r => r.json())
      .then(d => {
        setData(d)
        setLoading(false)
      })
      .catch(e => {
        console.error("Failed to load admin models", e)
        setLoading(false)
      })
  }, [])

  if (loading) {
    return <div className="text-[12px] text-text-muted mt-2">Загрузка моделей...</div>
  }

  if (!data) return null

  const aliases = Object.keys(data.model_map || {})

  return (
    <div className="mt-6 mb-6">
      <div className="text-[12px] font-semibold text-text-secondary uppercase tracking-wider mb-3.5 flex items-center gap-1.5">
        <span>Конфигурация моделей</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Aliases mapping */}
        <div className="bg-bg-card border border-white/[0.03] rounded-lg p-4">
          <h3 className="text-[13px] font-semibold text-text-primary mb-3">Настроенные алиасы (model_map)</h3>
          {aliases.length === 0 ? (
            <div className="text-[12px] text-text-muted">Нет настроенных алиасов</div>
          ) : (
            <div className="flex flex-col gap-2">
              {aliases.map(alias => (
                <div key={alias} className="flex flex-wrap items-center justify-between text-[12px] border-b border-white/[0.03] pb-2 last:border-0 last:pb-0">
                  <span className="font-mono text-[#00ff66]">{alias}</span>
                  <span className="text-text-muted text-[11px]">→ {data.model_map[alias]}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Available Models */}
        <div className="bg-bg-card border border-white/[0.03] rounded-lg p-4">
          <h3 className="text-[13px] font-semibold text-text-primary mb-3">Доступные внутренние модели</h3>
          {data.available_models && data.available_models.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {data.available_models.map(m => (
                <span key={m.id} className="text-[11px] px-2 py-0.5 bg-white/[.06] rounded text-text-secondary" title={m.id}>
                  {m.name || m.id}
                </span>
              ))}
            </div>
          ) : (
            <div className="text-[12px] text-text-muted">Нет доступных моделей</div>
          )}
        </div>
      </div>
    </div>
  )
}
