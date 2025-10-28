from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
import serial
import serial.tools.list_ports
import threading
import time
from datetime import datetime
import json

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key_here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///intrusion.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Database Model
class IntrusionEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(20), nullable=False)
    message = db.Column(db.String(200), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    duration = db.Column(db.Float)

    def to_dict(self):
        return {
            'id': self.id,
            'event_type': self.event_type,
            'message': self.message,
            'timestamp': self.timestamp.isoformat(),
            'duration': self.duration
        }

# Global variables
ser = None
serial_connected = False
reading = False
current_motion_start = None
calibration_start_time = None
calibration_dots_received = 0
total_calibration_time = 30  # From Arduino code

def init_db():
    with app.app_context():
        db.create_all()

def get_available_ports():
    ports = [port.device for port in serial.tools.list_ports.comports()]
    return ports

def connect_serial(port='COM10', baudrate=9600):
    global ser, serial_connected, reading, calibration_start_time, calibration_dots_received
    try:
        # Close existing connection if any
        if ser and ser.is_open:
            ser.close()
            time.sleep(1)
        
        print(f"🔌 Attempting to connect to {port} at {baudrate} baud...")
        
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=1,
            write_timeout=1,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE
        )
        
        # Clear any existing data in buffer
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        time.sleep(2)  # Wait for Arduino to reset
        
        serial_connected = True
        reading = True
        calibration_start_time = None
        calibration_dots_received = 0
        
        # Start reading thread
        thread = threading.Thread(target=read_serial_data, daemon=True)
        thread.start()
        
        print(f"✅ Successfully connected to {port}")
        socketio.emit('serial_status', {'status': 'connected', 'port': port})
        log_event('system', f"Connected to Arduino on {port}")
        return True
        
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Connection failed: {error_msg}")
        socketio.emit('serial_error', {'error': error_msg})
        log_event('system', f"Connection failed: {error_msg}")
        return False

def disconnect_serial():
    global ser, serial_connected, reading, calibration_start_time, calibration_dots_received
    reading = False
    serial_connected = False
    calibration_start_time = None
    calibration_dots_received = 0
    
    if ser and ser.is_open:
        ser.close()
        print("🔌 Disconnected from serial port")
    
    socketio.emit('serial_status', {'status': 'disconnected'})
    socketio.emit('calibration_progress', {'progress': 0, 'remaining': total_calibration_time, 'active': False})
    log_event('system', "Disconnected from Arduino")

def read_serial_data():
    global current_motion_start, calibration_start_time, calibration_dots_received
    buffer = ""
    
    while reading and ser and ser.is_open:
        try:
            # Read all available bytes
            if ser.in_waiting > 0:
                # Read byte by byte to avoid missing data
                while ser.in_waiting > 0:
                    byte = ser.read(1)
                    if byte:
                        char = byte.decode('utf-8', errors='ignore')
                        buffer += char
                        
                        # Process when we have a complete line or meaningful data
                        if char == '\n' or char == '\r':
                            line = buffer.strip()
                            if line:
                                print(f"📡 RAW FROM ARDUINO: '{line}'")
                                process_serial_message(line)
                            buffer = ""
                        # Process calibration dots in real-time
                        elif char == '.' and 'calibrating sensor' in buffer:
                            calibration_dots_received += 1
                            update_calibration_progress()
            
            time.sleep(0.1)  # Small delay to prevent CPU overload
            
        except Exception as e:
            if reading:
                print(f"❌ Serial read error: {str(e)}")
                log_event('system', f"Serial read error: {str(e)}")
            break

def update_calibration_progress():
    global calibration_dots_received
    if calibration_dots_received > 0:
        progress = min(100, (calibration_dots_received / total_calibration_time) * 100)
        remaining = max(0, total_calibration_time - calibration_dots_received)
        
        socketio.emit('calibration_progress', {
            'progress': progress,
            'remaining': remaining,
            'active': True,
            'dots': calibration_dots_received
        })

def process_serial_message(message):
    global current_motion_start, calibration_start_time, calibration_dots_received
    
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"🔄 Processing: {message}")
    
    # Emit raw message to frontend
    socketio.emit('serial_data', {
        'message': message,
        'timestamp': timestamp
    })
    
    # Parse Arduino messages - handle different message formats
    message_lower = message.lower()
    
    if "calibrating sensor" in message_lower:
        print("🎯 Detected: Sensor calibration started")
        calibration_start_time = datetime.now()
        calibration_dots_received = 0
        socketio.emit('sensor_status', {'status': 'calibrating'})
        log_event('system', 'Sensor calibration started...')
        
    elif "sensor active" in message_lower:
        print("🎯 Detected: Sensor active")
        socketio.emit('sensor_status', {'status': 'active'})
        socketio.emit('calibration_progress', {'progress': 100, 'remaining': 0, 'active': False})
        log_event('system', '✅ Sensor active and armed - Ready for detection')
        
    elif "motion detected" in message_lower:
        print("🎯 Detected: Motion start")
        socketio.emit('motion_status', {'status': 'detected', 'message': message})
        current_motion_start = datetime.now()
        
        # Save to database
        with app.app_context():
            event = IntrusionEvent(
                event_type='motion_start',
                message=message
            )
            db.session.add(event)
            db.session.commit()
        
        log_event('motion_start', '🚨 MOTION DETECTED!')
        
        # Emit motion alert for sound and notification
        socketio.emit('motion_alert', {
            'type': 'detected',
            'timestamp': timestamp,
            'message': 'Motion detected!'
        })
        
    elif "motion ended" in message_lower:
        print("🎯 Detected: Motion end")
        duration = None
        if current_motion_start:
            duration = (datetime.now() - current_motion_start).total_seconds()
            
        socketio.emit('motion_status', {
            'status': 'ended', 
            'message': message,
            'duration': duration
        })
        
        # Save to database
        with app.app_context():
            event = IntrusionEvent(
                event_type='motion_end',
                message=message,
                duration=duration
            )
            db.session.add(event)
            db.session.commit()
        
        duration_text = f' (Duration: {duration:.1f}s)' if duration else ''
        log_event('motion_end', f'Motion ended{duration_text}')
        current_motion_start = None
        
    else:
        # Log any other messages from Arduino
        print(f"📝 Other message: {message}")
        log_event('system', f"Arduino: {message}")

def log_event(event_type, message, duration=None):
    log_data = {
        'type': event_type,
        'message': message,
        'timestamp': datetime.now().isoformat(),
        'duration': duration
    }
    print(f"📢 Emitting log_update: {log_data}")
    socketio.emit('log_update', log_data)

# Flask Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/events')
def get_events():
    try:
        with app.app_context():
            events = IntrusionEvent.query.order_by(IntrusionEvent.timestamp.desc()).limit(50).all()
            return jsonify([event.to_dict() for event in events])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats')
def get_stats():
    try:
        with app.app_context():
            total_events = IntrusionEvent.query.filter_by(event_type='motion_start').count()
            
            today = datetime.now().date()
            today_events = IntrusionEvent.query.filter(
                IntrusionEvent.event_type == 'motion_start',
                db.func.date(IntrusionEvent.timestamp) == today
            ).count()
            
            last_event = IntrusionEvent.query.order_by(IntrusionEvent.timestamp.desc()).first()
            
            return jsonify({
                'total_events': total_events,
                'today_events': today_events,
                'last_event': last_event.to_dict() if last_event else None
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/ports')
def get_ports():
    ports = get_available_ports()
    print(f"🔍 Available ports: {ports}")
    return jsonify(ports)

@app.route('/api/connect', methods=['POST'])
def connect():
    data = request.json
    port = data.get('port', 'COM10')
    baudrate = data.get('baudrate', 9600)
    
    success = connect_serial(port, baudrate)
    return jsonify({'success': success})

@app.route('/api/disconnect', methods=['POST'])
def disconnect():
    disconnect_serial()
    return jsonify({'success': True})

@app.route('/api/clear_events', methods=['POST'])
def clear_events():
    try:
        with app.app_context():
            IntrusionEvent.query.delete()
            db.session.commit()
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# WebSocket Events
@socketio.on('connect')
def handle_connect():
    print('✅ Client connected to WebSocket')
    socketio.emit('connection_status', {'status': 'connected'})
    
    # Send current serial status
    if serial_connected:
        socketio.emit('serial_status', {'status': 'connected'})
    else:
        socketio.emit('serial_status', {'status': 'disconnected'})

@socketio.on('disconnect')
def handle_disconnect():
    print('❌ Client disconnected from WebSocket')

# Initialize and start the application
if __name__ == '__main__':
    print("🔧 Initializing database...")
    init_db()
    
    print("🚀 Starting IoT Intrusion Detection System...")
    print("💻 Access the dashboard at: http://localhost:5000")
    print("📱 For phone access, use: http://[YOUR_IP_ADDRESS]:5000")
    print("🔌 Serial connection: WAITING for frontend to initiate connection")
    
    # Start Flask application
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
