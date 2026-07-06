import { useState, useCallback, useEffect, useRef } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import Header from './components/Header.jsx';
import CloudBanner from './components/CloudBanner.jsx';
import NeighborhoodMap from './components/NeighborhoodMap.jsx';
import EventLog from './components/EventLog.jsx';
import CheckpointModal from './components/CheckpointModal.jsx';
import './App.css';

function getApiBase() {
  return import.meta.env.VITE_BACKEND_URL || 'http://localhost:8000';
}

const riskToSize = { normal: 0, watch: 20, warning: 45, emergency: 70 };

// Minimum ms between log entries for the same zone — prevents log flooding
const LOG_COOLDOWN_MS = 8000;

function App() {
  const [cloudAvailable, setCloudAvailable] = useState(null);
  const [isTogglingCloud, setIsTogglingCloud] = useState(false);
  const [events, setEvents] = useState([]);
  const [checkpointQueue, setCheckpointQueue] = useState([]);
  const [zoneRisks, setZoneRisks] = useState({ A: 'normal', B: 'normal', C: 'normal' });
  const [liveWaterSizes, setLiveWaterSizes] = useState({ A: 0, B: 0, C: 0 });
  const [overrides, setOverrides] = useState({ A: null, B: null, C: null });

  // Track last log time per zone to throttle entries
  const lastLogTime = useRef({ A: 0, B: 0, C: 0 });

  useEffect(() => {
    fetch(`${getApiBase()}/health`)
      .then((res) => res.json())
      .then((data) => setCloudAvailable(data.cloud_available ?? true))
      .catch(() => setCloudAvailable(true));
  }, []);

  const handleMessage = useCallback((msg) => {
    const now = Date.now();

    switch (msg.type) {
      case 'degradation_status': {
        setCloudAvailable(msg.cloud_available ?? false);
        // Always log degradation status changes
        setEvents((prev) => [{
          ...msg,
          id: `${now}-${Math.random().toString(36).substr(2, 9)}`
        }, ...prev].slice(0, 50));
        break;
      }

      case 'risk_decision': {
        const zone = msg.zone;
        const riskLevel = (msg.risk_level || 'normal').toLowerCase();

        if (zone) {
          setZoneRisks((prev) => ({ ...prev, [zone]: riskLevel }));
          setLiveWaterSizes((prev) => ({ ...prev, [zone]: riskToSize[riskLevel] ?? 0 }));

          // Throttle log entries per zone
          const lastTime = lastLogTime.current[zone] || 0;
          if (now - lastTime >= LOG_COOLDOWN_MS) {
            lastLogTime.current[zone] = now;
            setEvents((prev) => [{
              ...msg,
              id: `${now}-${Math.random().toString(36).substr(2, 9)}`
            }, ...prev].slice(0, 50));
          }
        }
        break;
      }

      case 'action_taken': {
        // Don't add to event log — risk_decision already covers it
        // Only handle checkpoint queue
        if (msg.requires_human_approval) {
          setCheckpointQueue((prev) => {
            const alreadyQueued = prev.some((item) => item.zone === msg.zone);
            if (alreadyQueued) return prev;
            return [...prev, { ...msg, id: `${now}-${Math.random().toString(36).substr(2, 9)}` }];
          });
        }
        break;
      }

      case 'qwen_call_started': {
        // Throttle these too — one per zone per cooldown window
        const zone = msg.zone;
        const lastTime = lastLogTime.current[zone] || 0;
        if (now - lastTime < LOG_COOLDOWN_MS) break; // skip if we just logged for this zone
        setEvents((prev) => [{
          ...msg,
          id: `${now}-${Math.random().toString(36).substr(2, 9)}`
        }, ...prev].slice(0, 50));
        break;
      }

      default:
        break;
    }
  }, []);

  useWebSocket(handleMessage);

  const handleApproveCheckpoint = useCallback(async (zone, checkpointEvent) => {
    const base = getApiBase();
    const res = await fetch(`${base}/approve/${zone}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });
    if (!res.ok) throw new Error(`Approval endpoint returned HTTP ${res.status}`);
    setCheckpointQueue((prev) => prev.filter((item) => item.id !== checkpointEvent.id));
  }, []);

  const handleDismissCheckpoint = useCallback(() => {
    setCheckpointQueue((prev) => prev.slice(1));
  }, []);

  const handleTriggerTestCheckpoint = useCallback((zone) => {
    const fakeEvent = {
      type: 'action_taken',
      zone,
      timestamp: Date.now() / 1000,
      id: `test-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
      action: 'WARNING',
      requires_human_approval: true,
      reasoning: `Simulated checkpoint: Verification request for flood gates and drainage pumps in Zone ${zone}.`,
      confidence: parseFloat((0.85 + Math.random() * 0.14).toFixed(2))
    };
    setCheckpointQueue((prev) => {
      const alreadyQueued = prev.some((item) => item.zone === zone);
      if (alreadyQueued) return prev;
      return [...prev, fakeEvent];
    });
  }, []);

  const handleSetOverride = useCallback((zone, val) => {
    setOverrides((prev) => ({ ...prev, [zone]: val }));
  }, []);

  const handleClearOverride = useCallback((zone) => {
    setOverrides((prev) => ({ ...prev, [zone]: null }));
  }, []);

  const handleToggleCloud = useCallback(async () => {
    if (cloudAvailable === null || isTogglingCloud) return;
    const nextState = !cloudAvailable;
    const endpoint = nextState ? '/cloud/on' : '/cloud/off';
    setIsTogglingCloud(true);
    setCloudAvailable(nextState);
    try {
      const res = await fetch(`${getApiBase()}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setCloudAvailable(cloudAvailable);
    } finally {
      setIsTogglingCloud(false);
    }
  }, [cloudAvailable, isTogglingCloud]);

  return (
    <div className="app-shell">
      <Header />
      <CloudBanner
        cloudAvailable={cloudAvailable}
        onToggleCloud={handleToggleCloud}
        isTogglingCloud={isTogglingCloud}
      />
      <main className="dashboard-grid">
        <section className="dashboard-map-panel">
          <NeighborhoodMap
            zoneRisks={zoneRisks}
            liveWaterSizes={liveWaterSizes}
            overrides={overrides}
            onSetOverride={handleSetOverride}
            onClearOverride={handleClearOverride}
            onTriggerTestCheckpoint={handleTriggerTestCheckpoint}
          />
        </section>
        <section className="dashboard-log-panel">
          <EventLog events={events} />
        </section>
      </main>
      {checkpointQueue.length > 0 && (
        <CheckpointModal
          activeCheckpoint={checkpointQueue[0]}
          queueCount={checkpointQueue.length}
          onApprove={handleApproveCheckpoint}
          onDismiss={handleDismissCheckpoint}
        />
      )}
    </div>
  );
}

export default App;