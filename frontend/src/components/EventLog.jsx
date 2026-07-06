import React from 'react';
import './EventLog.css';

/**
 * Formats a Unix timestamp (seconds) into HH:MM:SS format.
 */
function formatTime(timestamp) {
  if (!timestamp) return '--:--:--';
  const date = new Date(timestamp * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * EventLog Component
 * Displays a list of incoming WebSocket events, newest on top.
 */
function EventLog({ events }) {
  return (
    <div className="event-log-container">
      <div className="event-log-header">
        <h2 className="event-log-title">Live Event Log</h2>
        <span className="event-log-count">{events.length} / 50</span>
      </div>
      <div className="event-log-list">
        {events.length === 0 ? (
          <div className="event-log-empty">
            No events logged yet. Active monitoring.
          </div>
        ) : (
          events.map((event) => {
            const timeStr = formatTime(event.timestamp);
            const zone = event.zone || 'all';

            return (
              <div key={event.id} className={`event-log-item event-type-${event.type}`}>
                <div className="event-item-meta">
                  <span className="event-item-time">{timeStr}</span>
                  <span className={`event-item-zone zone-badge-${zone.toLowerCase()}`}>
                    Zone {zone}
                  </span>
                </div>
                <div className="event-item-content">
                  {renderEventBody(event)}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/**
 * Renders custom content per message type based on schema
 */
function renderEventBody(event) {
  const { type } = event;

  if (type === 'sensor_reading') {
    const sensorName = event.sensor.replace('_', ' ');
    return (
      <span className="sensor-body">
        {sensorName}: <strong className="sensor-value">{event.value}</strong> {event.unit}
        {event.flagged && <span className="flagged-indicator">⚠️ flagged</span>}
      </span>
    );
  }

  if (type === 'risk_decision') {
    const risk = event.risk_level || 'normal';
    const source = event.source || 'cloud';
    const confidence = event.confidence ? ` (conf: ${(event.confidence * 100).toFixed(0)}%)` : '';
    const recommended = event.recommended_actions?.length
      ? ` [Actions: ${event.recommended_actions.join(', ')}]`
      : '';
    const truncatedReason = event.reasoning
      ? event.reasoning.substring(0, 100) + (event.reasoning.length > 100 ? '...' : '')
      : 'No reasoning provided.';

    return (
      <div className="risk-body">
        <div className="risk-header-row">
          <span className={`risk-badge risk-${risk}`}>
            {risk.toUpperCase()}
          </span>
          <span className="risk-source">via {source}{confidence}</span>
        </div>
        <p className="risk-reasoning">{truncatedReason}</p>
        {recommended && <div className="risk-actions">{recommended}</div>}
      </div>
    );
  }

  if (type === 'action_taken') {
    return (
      <div className="action-body">
        <span className="action-label">Action:</span> <strong className="action-name">{event.action}</strong>
        {event.requires_human_approval && (
          <div className="checkpoint-warning">
            ⚠️ REQUIRES HUMAN APPROVAL CHECKPOINT
          </div>
        )}
      </div>
    );
  }

  if (type === 'degradation_status') {
    return (
      <span className={`degradation-body degradation-${event.cloud_available ? 'restored' : 'offline'}`}>
        {event.cloud_available ? 'CLOUD RESTORED' : 'CLOUD OFFLINE — ' + (event.cloud_state || 'local rules active')}
      </span>
    );
  }

  if (type === 'qwen_call_started') {
    const sensors = event.flagged ? Object.keys(event.flagged).join(', ') : 'multiple';
    return (
      <span className="qwen-started-body">
        Qwen reasoning... <span className="muted-sensors">(trigger: {sensors})</span>
      </span>
    );
  }

  return <span className="default-body">{JSON.stringify(event)}</span>;
}

export default EventLog;
