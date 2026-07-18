import React, { useState } from 'react';
import './CheckpointModal.css';

function CheckpointModal({ activeCheckpoint, queueCount, onApprove, onDismiss }) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);

  if (!activeCheckpoint) return null;

  const { zone } = activeCheckpoint;

  // Backend sends fields flat (not nested under payload)
  const message = activeCheckpoint.reasoning || activeCheckpoint.payload?.message || 'Action requires authorization check.';
  const actionName = activeCheckpoint.action || activeCheckpoint.risk_level || activeCheckpoint.payload?.action || 'Unknown Action';
  const confidence = activeCheckpoint.confidence ?? activeCheckpoint.payload?.confidence;

  const handleApproveClick = async () => {
    setIsSubmitting(true);
    setErrorMsg(null);
    try {
      await onApprove(zone, activeCheckpoint);
    } catch (err) {
      setErrorMsg(err.message || 'Failed to approve checkpoint.');
      setIsSubmitting(false);
    }
  };

  return (
    <div className="checkpoint-modal-overlay">
      <div className="checkpoint-modal-card" role="dialog" aria-modal="true">
        <div className="checkpoint-modal-header">
          <div className="checkpoint-header-left">
            <span className="checkpoint-header-pulse-icon">⚠️</span>
            <h3 className="checkpoint-modal-title">Human Checkpoint Required</h3>
          </div>
          {queueCount > 1 && (
            <span className="checkpoint-queue-badge">
              1 of {queueCount} queued
            </span>
          )}
        </div>

        <div className="checkpoint-modal-body">
          <div className="checkpoint-detail-grid">
            <div className="checkpoint-detail-row">
              <span className="checkpoint-detail-label">Target Zone</span>
              <span className={`checkpoint-detail-val zone-text-${zone?.toLowerCase()}`}>
                Zone {zone}
              </span>
            </div>

            <div className="checkpoint-detail-row">
              <span className="checkpoint-detail-label">Requested Action</span>
              <span className="checkpoint-detail-val action-highlight">
                {actionName.replace(/_/g, ' ').toUpperCase()}
              </span>
            </div>

            {typeof confidence === 'number' && (
              <div className="checkpoint-detail-row">
                <span className="checkpoint-detail-label">Confidence Score</span>
                <span className="checkpoint-detail-val confidence-score">
                  {(confidence * 100).toFixed(0)}%
                </span>
              </div>
            )}
          </div>

          <div className="checkpoint-message-container">
            <h4 className="checkpoint-message-heading">System Reasoning Message:</h4>
            <p className="checkpoint-message-text">{message}</p>
          </div>

          {errorMsg && (
            <div className="checkpoint-error-box">
              Error: {errorMsg}
            </div>
          )}
        </div>

        <div className="checkpoint-modal-footer">
          <button
            className="checkpoint-btn-dismiss"
            onClick={onDismiss}
            disabled={isSubmitting}
          >
            Dismiss
          </button>
          <button
            className="checkpoint-btn-approve"
            onClick={handleApproveClick}
            disabled={isSubmitting}
          >
            {isSubmitting ? 'Approving…' : 'Approve Action'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default CheckpointModal;