import React, { useState } from 'react';
import './DemoControlPanel.css';

const SCENARIOS = [
  { id: 'normal',      label: 'Normal',      icon: '🟢', desc: 'All clear' },
  { id: 'light_rain',  label: 'Light Rain',  icon: '🌧️', desc: 'Watch level' },
  { id: 'heavy_storm', label: 'Heavy Storm', icon: '⛈️', desc: 'Warning level' },
  { id: 'flash_flood', label: 'Flash Flood', icon: '🚨', desc: 'Emergency' },
];

function DemoControlPanel({ apiBase }) {
  const [activeScenario, setActiveScenario] = useState(null);
  const [cloudState, setCloudState] = useState('on');
  const [loading, setLoading] = useState(null);

  const setScenario = async (scenarioId) => {
    setLoading(scenarioId);
    try {
      await fetch(`${apiBase}/scenario/${scenarioId}`, { method: 'POST' });
      setActiveScenario(scenarioId);
    } catch (e) {
      console.error('Scenario switch failed:', e);
    } finally {
      setLoading(null);
    }
  };

  const toggleCloud = async (state) => {
    setLoading(`cloud_${state}`);
    try {
      await fetch(`${apiBase}/cloud/${state}`, { method: 'POST' });
      setCloudState(state);
    } catch (e) {
      console.error('Cloud toggle failed:', e);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="demo-panel">
      <div className="demo-panel-section">
        <span className="demo-panel-label">Scenario</span>
        <div className="demo-scenario-grid">
          {SCENARIOS.map((s) => (
            <button
              key={s.id}
              className={`demo-scenario-btn ${activeScenario === s.id ? 'demo-scenario-btn--active' : ''} demo-scenario-btn--${s.id}`}
              onClick={() => setScenario(s.id)}
              disabled={loading === s.id}
            >
              <span className="demo-scenario-icon">{s.icon}</span>
              <span className="demo-scenario-label">{s.label}</span>
              <span className="demo-scenario-desc">{s.desc}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="demo-panel-divider" />

      <div className="demo-panel-section">
        <span className="demo-panel-label">Edge-Cloud Link</span>
        <div className="demo-cloud-btns">
          <button
            className={`demo-cloud-btn demo-cloud-btn--on ${cloudState === 'on' ? 'demo-cloud-btn--active' : ''}`}
            onClick={() => toggleCloud('on')}
            disabled={loading === 'cloud_on' || cloudState === 'on'}
          >
            ☁️ Cloud On
          </button>
          <button
            className={`demo-cloud-btn demo-cloud-btn--off ${cloudState === 'off' ? 'demo-cloud-btn--active' : ''}`}
            onClick={() => toggleCloud('off')}
            disabled={loading === 'cloud_off' || cloudState === 'off'}
          >
            ✂️ Cut Cloud
          </button>
        </div>
        {cloudState === 'off' && (
          <p className="demo-cloud-hint">
            Edge agent now running local rules. Watch degradation states below.
          </p>
        )}
      </div>
    </div>
  );
}

export default DemoControlPanel;
