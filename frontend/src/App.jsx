import { useState, useCallback, useEffect } from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import Header from './components/Header.jsx';
import CloudBanner from './components/CloudBanner.jsx';
import NeighborhoodMap from './components/NeighborhoodMap.jsx';
import EventLog from './components/EventLog.jsx';
import CheckpointModal from './components/CheckpointModal.jsx';
import './App.css';

/** Returns the REST base URL from the environment variable. */
function getApiBase() {
  return import.meta.env.VITE_BACKEND_URL || 'http://localhost:8000';
}

const riskToSize = { normal: 0, watch: 20, warning: 45, emergency: 70 };

function App() {
  const [cloudAvailable, setCloudAvailable] = useState(null);
  const [isTogglingCloud, setIsTogglingCloud] = useState(false);
  const [events, setEvents] = useState([]);
  const [checkpointQueue, setCheckpointQueue] = useState([]);
  const [zoneRisks, setZoneRisks] = useState({
    A: 'normal',
    B: 'normal',
    C: 'normal'
  });
  const [liveWaterSizes, setLiveWaterSizes] = useState({
    A: 0,
    B: 0,
    C: 0
  });
  const [overrides, setOverrides] = useState({
    A: null,
    B: null,
    C: null
  });

  useEffect(() => {
    fetch('/health')
      .then((res) => {
        if (!res.ok) throw new Error(`Health check HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        console.log('[FloodGuard] Initial health check ->', data);
        setCloudAvailable(data.cloud_available ?? true);
      })
      .catch((err) => {
        console.warn('[FloodGuard] Health check failed, defaulting cloud to online:', err.message);
        setCloudAvailable(true);
      });
  }, []);

  const handleMessage = useCallback((msg) => {
    const newLogEntry = {
      ...msg,
      id: `${msg.timestamp || Date.now()}-${Math.random().toString(36).substr(2, 9)}`
    };

    setEvents((prevEvents) => [newLogEntry, ...prevEvents].slice(0, 50));

    switch (msg.type) {
      case 'degradation_status': {
        setCloudAvailable(msg.payload?.cloud_available ?? false);
        break;
      }

      case 'risk_decision': {
        const { zone, payload } = msg;
        if (zone && payload) {
          const riskLevel = payload.risk_level || 'normal';

          setZoneRisks((prevRisks) => ({
            ...prevRisks,
            [zone]: riskLevel
          }));

          const size = riskToSize[riskLevel] ?? 0;
          setLiveWaterSizes((prevSizes) => ({
            ...prevSizes,
            [zone]: size
          }));
        }
        break;
      }

      case 'action_taken': {
        if (msg.payload?.requires_human_approval) {
          setCheckpointQueue((prevQueue) => [...prevQueue, newLogEntry]);
        }
        break;
      }

      default:
        break;
    }
  }, []);

  useWebSocket(handleMessage);

  const handleApproveCheckpoint = useCallback(async (zone, checkpointEvent) => {
    const base = getApiBase();
    console.log(`[Checkpoint] Sending approval request for Zone ${zone}...`);

    const res = await fetch(`${base}/approve/${zone}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      }
    });

    if (!res.ok) {
      throw new Error(`REST approval endpoint returned HTTP ${res.status}`);
    }

    console.log(`[Checkpoint] Zone ${zone} successfully approved ✓`);
    setCheckpointQueue((prevQueue) => prevQueue.filter((item) => item.id !== checkpointEvent.id));
  }, []);

  const handleDismissCheckpoint = useCallback(() => {
    setCheckpointQueue((prevQueue) => prevQueue.slice(1));
  }, []);

  const handleTriggerTestCheckpoint = useCallback((zone) => {
    const fakeEvent = {
      type: 'action_taken',
      zone,
      timestamp: Date.now() / 1000,
      id: `test-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
      payload: {
        action: 'human_checkpoint_raised',
        requires_human_approval: true,
        message: `Simulated checkpoint: Verification request for flood gates and drainage pumps status in Zone ${zone}. Please inspect water levels and authorize broadcast.`,
        confidence: parseFloat((0.85 + Math.random() * 0.14).toFixed(2))
      }
    };

    setEvents((prev) => [fakeEvent, ...prev].slice(0, 50));
    setCheckpointQueue((prevQueue) => [...prevQueue, fakeEvent]);
  }, []);

  const handleSetOverride = useCallback((zone, val) => {
    setOverrides((prev) => ({
      ...prev,
      [zone]: val
    }));
  }, []);

  const handleClearOverride = useCallback((zone) => {
    setOverrides((prev) => ({
      ...prev,
      [zone]: null
    }));
  }, []);

  const handleToggleCloud = useCallback(async () => {
    if (cloudAvailable === null || isTogglingCloud) {
      return;
    }

    const nextCloudState = !cloudAvailable;
    const endpoint = nextCloudState ? '/cloud/on' : '/cloud/off';

    setIsTogglingCloud(true);
    setCloudAvailable(nextCloudState);

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        }
      });

      if (!res.ok) {
        throw new Error(`Cloud toggle endpoint returned HTTP ${res.status}`);
      }
    } catch (err) {
      console.warn('[FloodGuard] Cloud toggle failed, reverting state:', err.message);
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
