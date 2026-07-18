import React, { useState, useMemo } from 'react';
import floodMapBg from '../assets/flood-map-bg.png';
import './NeighborhoodMap.css';

const ZONE_CONFIG = {
  A: { dotTop: '15%', dotLeft: '20%', waterTop: '20%', waterLeft: '8%',  waterDirection: 'right' },
  B: { dotTop: '48%', dotLeft: '40%', waterTop: '46%', waterLeft: '28%', waterDirection: 'right' },
  C: { dotTop: '78%', dotLeft: '55%', waterTop: '74%', waterLeft: '42%', waterDirection: 'right' },
};

// Percentage-based dimensions so water scales with map container
const WATER_BASE = {
  normal:    { width: '0%',   height: '0%'  },
  watch:     { width: '9%',   height: '6%'  },
  warning:   { width: '18%',  height: '10%' },
  emergency: { width: '28%',  height: '15%' },
};

function NeighborhoodMap({ zoneRisks, liveWaterSizes, overrides, onSetOverride, onClearOverride, onTriggerTestCheckpoint }) {
  const [devPanelOpen, setDevPanelOpen] = useState(false);

  const getRiskLevelFromSize = (size) => {
    if (size >= 75) return 'emergency';
    if (size >= 50) return 'warning';
    if (size >= 25) return 'watch';
    return 'normal';
  };

  const zoneVisuals = useMemo(() => {
    const get = (zoneName) => {
      const overrideVal = overrides[zoneName];
      if (overrideVal !== null) return { risk: getRiskLevelFromSize(overrideVal), size: overrideVal };
      const risk = zoneRisks[zoneName] || 'normal';
      return { risk, size: liveWaterSizes[zoneName] ?? 0 };
    };
    return { A: get('A'), B: get('B'), C: get('C') };
  }, [zoneRisks, liveWaterSizes, overrides]);

  const highestRisk = useMemo(() => {
    const p = { emergency: 4, warning: 3, watch: 2, normal: 1 };
    return Object.values(zoneVisuals).reduce((max, { risk }) => p[risk] > p[max] ? risk : max, 'normal');
  }, [zoneVisuals]);

  const rainConfig = useMemo(() => {
    switch (highestRisk) {
      case 'emergency': return { count: 96, overlayOpacity: 0.86, speedMin: 0.45, speedMax: 0.65, dropOpacityMin: 0.48, dropOpacityMax: 0.7,  lengthMin: 76, lengthMax: 92 };
      case 'warning':   return { count: 60, overlayOpacity: 0.72, speedMin: 0.65, speedMax: 0.85, dropOpacityMin: 0.4,  dropOpacityMax: 0.58, lengthMin: 72, lengthMax: 88 };
      case 'watch':     return { count: 36, overlayOpacity: 0.6,  speedMin: 0.85, speedMax: 1.1,  dropOpacityMin: 0.34, dropOpacityMax: 0.5,  lengthMin: 68, lengthMax: 82 };
      default: return { count: 0, overlayOpacity: 0, speedMin: 1.2, speedMax: 1.5, dropOpacityMin: 0, dropOpacityMax: 0, lengthMin: 64, lengthMax: 78 };
    }
  }, [highestRisk]);

  const rainDrops = useMemo(() => {
    return Array.from({ length: rainConfig.count }).map((_, i) => {
      const seed = Math.sin(i + 17) * 10000;
      const r = seed - Math.floor(seed);
      return {
        id: i,
        left: `${(i * (100 / rainConfig.count)) + (r * 2 - 1)}%`,
        delay: `${r * 1.5}s`,
        duration: `${rainConfig.speedMin + r * (rainConfig.speedMax - rainConfig.speedMin)}s`,
        opacity: rainConfig.dropOpacityMin + r * (rainConfig.dropOpacityMax - rainConfig.dropOpacityMin),
        length: rainConfig.lengthMin + r * (rainConfig.lengthMax - rainConfig.lengthMin),
      };
    });
  }, [rainConfig]);

  return (
    <div className="map-container">
      <img src={floodMapBg} alt="Neighborhood Flood Map" className="map-background-img" />

      <div className="rain-overlay" style={{ opacity: rainConfig.overlayOpacity }}>
        {rainDrops.map((drop) => (
          <div key={drop.id} className="rain-drop" style={{
            left: drop.left, animationDelay: drop.delay,
            animationDuration: drop.duration, opacity: drop.opacity, height: `${drop.length}px`
          }} />
        ))}
      </div>

      {Object.entries(ZONE_CONFIG).map(([zoneName, config]) => {
        const { risk, size } = zoneVisuals[zoneName];
        const isOverridden = overrides[zoneName] !== null;
        const waterDims = WATER_BASE[risk] || WATER_BASE.normal;

        return (
          <React.Fragment key={zoneName}>
            {risk !== 'normal' && (
              <div
                className={`water-overlay water-dir-${config.waterDirection}`}
                style={{
                  top: config.waterTop,
                  left: config.waterLeft,
                  width: waterDims.width,
                  height: waterDims.height,
                  opacity: 0.85,
                }}
              />
            )}

            <div className="zone-marker-container" style={{ top: config.dotTop, left: config.dotLeft }}>
              <div className={`zone-dot risk-${risk}`} />
              <div className="zone-info-tooltip">
                <div className="tooltip-title">Zone {zoneName}</div>
                <div className="tooltip-detail">Risk: <span className={`risk-text-${risk}`}>{risk}</span></div>
                <div className="tooltip-detail">Water: {size}%</div>
                {isOverridden && <div className="tooltip-override-tag">Override Active</div>}
              </div>
              <div className="zone-label-badge">Zone {zoneName}</div>
            </div>
          </React.Fragment>
        );
      })}

      <div className={`dev-panel ${devPanelOpen ? 'dev-panel--open' : ''}`}>
        <button className="dev-panel-toggle" onClick={() => setDevPanelOpen(!devPanelOpen)}>
          {devPanelOpen ? 'Close Dev Panel' : 'dev override - testing only'}
        </button>

        {devPanelOpen && (
          <div className="dev-panel-content">
            <h3 className="dev-panel-title">Water Level Overrides</h3>
            {Object.keys(ZONE_CONFIG).map((zoneName) => {
              const overrideVal = overrides[zoneName];
              const isOverridden = overrideVal !== null;
              const liveVal = liveWaterSizes[zoneName] ?? 0;
              const displayVal = isOverridden ? overrideVal : liveVal;
              const activeRisk = isOverridden ? getRiskLevelFromSize(overrideVal) : (zoneRisks[zoneName] || 'normal');
              return (
                <div key={zoneName} className="dev-override-row">
                  <div className="dev-row-header">
                    <span className="dev-zone-name">Zone {zoneName}</span>
                    <span className="dev-zone-status">
                      {isOverridden ? `Override: ${overrideVal}% (${activeRisk})` : `Live: ${Math.round(liveVal)}% (${activeRisk})`}
                    </span>
                  </div>
                  <input type="range" min="0" max="100" value={displayVal} className="dev-slider"
                    onChange={(e) => onSetOverride(zoneName, parseInt(e.target.value, 10))} />
                  {isOverridden && (
                    <button className="dev-resume-btn" onClick={() => onClearOverride(zoneName)}>Resume Live</button>
                  )}
                </div>
              );
            })}
            {onTriggerTestCheckpoint && (
              <div className="dev-checkpoint-row">
                <span className="dev-panel-label">Trigger Test Checkpoint:</span>
                <div className="dev-btn-group">
                  {['A','B','C'].map(z => (
                    <button key={z} className="dev-test-btn" onClick={() => onTriggerTestCheckpoint(z)}>Zone {z}</button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default NeighborhoodMap;