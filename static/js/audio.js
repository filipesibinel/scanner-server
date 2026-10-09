// ============================================================================
// FILE: audio.js
// Audio feedback system for card scanner
// Uses Web Audio API for sound generation (no external files needed)
// ============================================================================

class AudioManager {
    constructor() {
        this.audioContext = null;
        this.enabled = true;
        this.volume = 0.3; // Default 30% volume
        this.muted = {}; // Single sounds switched off: 'capture', 'added'
        this.initAudioContext();
    }

    /**
     * Initialize Web Audio API context
     * Note: Must be called after user interaction due to browser autoplay policies
     */
    initAudioContext() {
        try {
            // Create audio context on first user interaction
            if (!this.audioContext) {
                this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            }
        } catch (e) {
            console.warn('Web Audio API not supported:', e);
            this.enabled = false;
        }
    }

    /**
     * Ensure audio context is running
     * Some browsers require user interaction to start audio
     */
    async ensureAudioContext() {
        if (!this.audioContext) {
            this.initAudioContext();
        }

        if (this.audioContext && this.audioContext.state === 'suspended') {
            try {
                await this.audioContext.resume();
            } catch (e) {
                console.warn('Could not resume audio context:', e);
            }
        }
    }

    /**
     * Play a beep sound
     * @param {number} frequency - Frequency in Hz (e.g., 440 = A4 note)
     * @param {number} duration - Duration in seconds
     * @param {string} type - Waveform type: 'sine', 'square', 'sawtooth', 'triangle'
     */
    async playBeep(frequency = 440, duration = 0.1, type = 'sine') {
        console.log(`🔊 playBeep called: freq=${frequency}Hz, dur=${duration}s, type=${type}, enabled=${this.enabled}, volume=${this.volume}`);

        if (!this.enabled) {
            console.log('🔇 Audio disabled, skipping beep');
            return;
        }

        if (!this.audioContext) {
            console.warn('⚠️ No audio context, attempting to initialize...');
            this.initAudioContext();
        }

        await this.ensureAudioContext();

        if (!this.audioContext) {
            console.error('❌ Failed to initialize audio context');
            return;
        }

        console.log(`🎵 Audio context state: ${this.audioContext.state}`);

        try {
            const oscillator = this.audioContext.createOscillator();
            const gainNode = this.audioContext.createGain();

            oscillator.connect(gainNode);
            gainNode.connect(this.audioContext.destination);

            oscillator.frequency.value = frequency;
            oscillator.type = type;

            // Envelope for smooth sound (prevents clicking)
            const now = this.audioContext.currentTime;
            gainNode.gain.setValueAtTime(0, now);
            gainNode.gain.linearRampToValueAtTime(this.volume, now + 0.01); // Attack
            gainNode.gain.linearRampToValueAtTime(this.volume * 0.3, now + duration - 0.01); // Sustain
            gainNode.gain.linearRampToValueAtTime(0, now + duration); // Release

            oscillator.start(now);
            oscillator.stop(now + duration);
            console.log('✅ Beep scheduled successfully');
        } catch (e) {
            console.error('❌ Error playing beep:', e);
        }
    }

    /**
     * Play capture sound (camera shutter click)
     */
    async playCapture() {
        if (this.muted.capture) return;
        // VERY loud double-click for immediate feedback
        await this.playBeep(1200, 0.10, 'square');
        setTimeout(() => this.playBeep(1000, 0.08, 'square'), 100);
    }

    /**
     * Play success sound (pleasant ding)
     */
    async playSuccess() {
        // Two-tone ding: low to high (louder for batch mode)
        await this.playBeep(523, 0.12, 'sine'); // C5
        setTimeout(() => this.playBeep(659, 0.20, 'sine'), 100); // E5
    }

    /**
     * Play the sound of a card added to the scanned cards (the success ding, with its own switch)
     */
    async playAdded() {
        if (!this.muted.added) await this.playSuccess();
    }

    /**
     * Play "next card ready" sound for fast scan mode
     */
    async playNextReady() {
        // Quick triple ascending beep to signal readiness
        await this.playBeep(880, 0.06, 'sine'); // A5
        setTimeout(() => this.playBeep(1047, 0.06, 'sine'), 60); // C6
        setTimeout(() => this.playBeep(1319, 0.10, 'sine'), 120); // E6
    }

    /**
     * Play error sound (subtle bonk)
     */
    async playError() {
        // Low frequency, short duration
        await this.playBeep(200, 0.15, 'square');
    }

    /**
     * Play queue alert sound
     */
    async playQueueAlert() {
        // Triple beep pattern
        for (let i = 0; i < 3; i++) {
            setTimeout(() => this.playBeep(880, 0.1, 'sine'), i * 150);
        }
    }

    /**
     * Play batch mode start sound
     */
    async playBatchModeStart() {
        // Rising tone
        await this.playBeep(440, 0.1, 'sine');
        setTimeout(() => this.playBeep(554, 0.1, 'sine'), 100);
        setTimeout(() => this.playBeep(659, 0.15, 'sine'), 200);
    }

    /**
     * Play batch mode stop sound
     */
    async playBatchModeStop() {
        // Falling tone
        await this.playBeep(659, 0.1, 'sine');
        setTimeout(() => this.playBeep(554, 0.1, 'sine'), 100);
        setTimeout(() => this.playBeep(440, 0.15, 'sine'), 200);
    }

    /**
     * Set volume (0.0 to 1.0)
     */
    setVolume(volume) {
        this.volume = Math.max(0, Math.min(1, volume));
    }

    /**
     * Enable/disable audio
     */
    setEnabled(enabled) {
        this.enabled = enabled;
    }

    /**
     * Switch single sounds off ('capture': the capture beep, 'added': the ding of an added card)
     */
    setMuted(sound, muted) {
        this.muted[sound] = muted;
    }

    /**
     * Get current settings
     */
    getSettings() {
        return {
            enabled: this.enabled,
            volume: this.volume,
            supported: !!this.audioContext
        };
    }
}

// Create global instance
const audioManager = new AudioManager();

// Initialize on first user interaction
document.addEventListener('click', () => {
    audioManager.ensureAudioContext();
}, { once: true });
