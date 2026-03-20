#!/usr/bin/env python3
"""
Quiz Formateur — Serveur en ligne multi-formateurs
Les comptes formateurs sont stockes dans data.json (ajout sans toucher au code).
"""

import json
import queue
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT       = int(os.environ.get('PORT', 3000))
PUBLIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public')
DATA_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.json')

# ── Data persistence ──────────────────────────────────────────────────────
# Structure of data.json:
# {
#   "trainers": {
#     "sylvain": {"name": "Sylvain", "password": "sylvain2025"},
#     "invite":  {"name": "Formateur Invité", "password": "invite2025"}
#   },
#   "formations": {
#     "sylvain": {"Ma formation": [questions...]},
#     "invite":  {}
#   }
# }
# To add a new trainer: just add an entry in "trainers".

state_lock = threading.RLock()

DEFAULT_DATA = {
    'trainers': {
        'sylvain': {'name': 'Sylvain', 'password': 'Sylvain'},
        'invite':  {'name': 'Formateur Invite', 'password': 'Formateur Invite'},
    },
    'formations': {
        'sylvain': {},
        'invite':  {},
    }
}


def load_data():
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                # Always use trainers from code (passwords etc.)
                data['trainers'] = DEFAULT_DATA['trainers']
                if 'formations' not in data:
                    data['formations'] = {}
                for tid in data['trainers']:
                    if tid not in data['formations']:
                        data['formations'][tid] = {}
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return json.loads(json.dumps(DEFAULT_DATA))


def save_data():
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)


all_data = load_data()

# ── Per-room state (one room per trainer) ─────────────────────────────────
rooms = {}
for tid in all_data['trainers']:
    rooms[tid] = {
        'session': {'phase': 'idle', 'currentIndex': -1, 'answers': {}},
        'formation': None,
        'questions': [],
    }

# ── Clients ───────────────────────────────────────────────────────────────
clients = {}


def _enqueue(c, event):
    try:
        c['queue'].put_nowait(event)
    except queue.Full:
        pass


def push(client_id, event):
    with state_lock:
        if client_id in clients:
            _enqueue(clients[client_id], event)


def push_to_room(room_id, role_filter, event):
    with state_lock:
        for c in clients.values():
            if c.get('room') == room_id:
                if role_filter is None or c['role'] == role_filter:
                    _enqueue(c, event)


def push_trainers(room_id, event):
    push_to_room(room_id, 'trainer', event)


def push_students(room_id, event):
    push_to_room(room_id, 'student', event)


def push_all(room_id, event):
    push_to_room(room_id, None, event)


def get_student_list(room_id):
    with state_lock:
        room = rooms.get(room_id)
        if not room:
            return []
        return [
            {'id': cid, 'name': c['name'], 'answered': cid in room['session']['answers']}
            for cid, c in clients.items()
            if c['role'] == 'student' and c.get('room') == room_id
        ]


def compute_stats(room_id):
    with state_lock:
        room = rooms.get(room_id)
        if not room:
            return None
        session = room['session']
        questions = room['questions']
        if session['currentIndex'] < 0 or session['currentIndex'] >= len(questions):
            return None
        q = questions[session['currentIndex']]
        counts = [0] * len(q['options'])
        for idx in session['answers'].values():
            if 0 <= idx < len(counts):
                counts[idx] += 1
        answered = len(session['answers'])
        total = sum(1 for c in clients.values()
                    if c['role'] == 'student' and c.get('room') == room_id)
        percs = [round(c / answered * 100) if answered > 0 else 0 for c in counts]
        return {
            'totalStudents': total,
            'answeredCount': answered,
            'answeredPercent': round(answered / total * 100) if total > 0 else 0,
            'counts': counts,
            'percentages': percs,
            'correctIndex': q['correctIndex'] if session['phase'] == 'revealed' else None,
        }


# ── HTTP Handler ──────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip('/') or '/'
        params = parse_qs(parsed.query)

        if path in ('/', '/student'):
            self._serve_file('student.html')
        elif path == '/trainer':
            self._serve_file('trainer.html')
        elif path == '/events':
            self._handle_sse(params)
        elif path == '/trainers-list':
            self._serve_trainers_list()
        elif path == '/export':
            self._handle_export(params)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_trainers_list(self):
        """Return public trainer list (id + name, no passwords)."""
        with state_lock:
            trainers = [
                {'id': tid, 'name': info['name']}
                for tid, info in all_data['trainers'].items()
            ]
        data = json.dumps(trainers, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _handle_export(self, params):
        """Export all formations+questions for a trainer as JSON download."""
        trainer_id = (params.get('trainerId') or [''])[0]
        password = (params.get('password') or [''])[0]
        trainers = all_data['trainers']
        if trainer_id not in trainers or password != trainers[trainer_id]['password']:
            self.send_response(403)
            self.end_headers()
            return
        with state_lock:
            formations = all_data['formations'].get(trainer_id, {})
            export = {
                'trainerId': trainer_id,
                'trainerName': trainers[trainer_id]['name'],
                'formations': formations,
            }
        data = json.dumps(export, ensure_ascii=False, indent=2).encode('utf-8')
        filename = 'quiz-formateur-' + trainer_id + '.json'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.send_header('Content-Length', str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            msg = json.loads(body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        self._handle_action(msg)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _serve_file(self, filename):
        filepath = os.path.join(PUBLIC_DIR, filename)
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _handle_sse(self, params):
        client_id  = (params.get('clientId') or [None])[0]
        role       = (params.get('role') or [''])[0]
        name       = (params.get('name') or [''])[0].strip()[:25]
        password   = (params.get('password') or [''])[0]
        room_id    = (params.get('room') or [''])[0]
        trainer_id = (params.get('trainerId') or [''])[0]

        if not client_id:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self._cors()
        self.end_headers()

        q = queue.Queue(maxsize=200)

        if role == 'trainer':
            trainers = all_data['trainers']
            if trainer_id not in trainers:
                self._sse({'type': 'auth-failed'})
                return
            if password != trainers[trainer_id]['password']:
                self._sse({'type': 'auth-failed'})
                return

            room_id = trainer_id
            # Ensure room exists (in case trainer was added at runtime)
            if room_id not in rooms:
                rooms[room_id] = {
                    'session': {'phase': 'idle', 'currentIndex': -1, 'answers': {}},
                    'formation': None,
                    'questions': [],
                }

            with state_lock:
                clients[client_id] = {'queue': q, 'role': 'trainer', 'name': None, 'room': room_id}
                room = rooms[room_id]
                formations = list(all_data['formations'].get(trainer_id, {}).keys())

            self._sse({
                'type': 'trainer-ok',
                'trainerId': trainer_id,
                'trainerName': trainers[trainer_id]['name'],
                'formations': formations,
                'currentFormation': room['formation'],
                'questions': room['questions'],
                'session': room['session'],
                'students': get_student_list(room_id),
                'stats': compute_stats(room_id),
            })

        elif role == 'student':
            if not name:
                self._sse({'type': 'error', 'message': 'Nom invalide'})
                return
            if room_id not in rooms:
                self._sse({'type': 'error', 'message': 'Salle introuvable'})
                return

            with state_lock:
                taken = any(
                    c['role'] == 'student'
                    and c.get('room') == room_id
                    and (c.get('name') or '').lower() == name.lower()
                    and cid != client_id
                    for cid, c in clients.items()
                )
                if taken:
                    self._sse({'type': 'name-taken'})
                    return

                clients[client_id] = {'queue': q, 'role': 'student', 'name': name, 'room': room_id}
                room = rooms[room_id]
                session = room['session']
                questions = room['questions']
                phase = session['phase']
                my_answer = session['answers'].get(client_id)
                correct_idx = None
                current_q = None
                if session['currentIndex'] >= 0 and phase != 'idle' and session['currentIndex'] < len(questions):
                    sq = questions[session['currentIndex']]
                    current_q = {
                        'index': session['currentIndex'],
                        'total': len(questions),
                        'text': sq['text'],
                        'options': sq['options'],
                    }
                    if phase == 'revealed':
                        correct_idx = sq['correctIndex']

            self._sse({
                'type': 'student-ok',
                'name': name,
                'phase': phase,
                'currentQuestion': current_q,
                'myAnswer': my_answer,
                'correctIndex': correct_idx,
            })
            push_trainers(room_id, {'type': 'students-update', 'students': get_student_list(room_id)})

        else:
            self._sse({'type': 'error', 'message': 'Role invalide'})
            return

        try:
            while True:
                try:
                    event = q.get(timeout=20)
                    self._sse(event)
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with state_lock:
                c_info = clients.pop(client_id, {})
                was_student = c_info.get('role') == 'student'
                c_room = c_info.get('room')
            if was_student and c_room:
                push_trainers(c_room, {'type': 'students-update', 'students': get_student_list(c_room)})

    def _sse(self, data):
        line = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
        self.wfile.write(line.encode('utf-8'))
        self.wfile.flush()

    def _handle_action(self, msg):
        t = msg.get('type')
        client_id = msg.get('clientId')
        if not client_id:
            return

        with state_lock:
            c_info = clients.get(client_id, {})
            role = c_info.get('role')
            room_id = c_info.get('room')

        if not room_id or room_id not in rooms:
            return

        room = rooms[room_id]

        # ── Student actions ───────────────────────────────────────────
        if t == 'student-answer':
            if role != 'student':
                return
            with state_lock:
                session = room['session']
                questions = room['questions']
                if session['phase'] != 'active':
                    push(client_id, {'type': 'not-active'})
                    return
                if client_id in session['answers']:
                    push(client_id, {'type': 'already-answered'})
                    return
                try:
                    opt_index = int(msg.get('optionIndex', -1))
                except (TypeError, ValueError):
                    return
                if session['currentIndex'] < 0 or session['currentIndex'] >= len(questions):
                    return
                q = questions[session['currentIndex']]
                if opt_index < 0 or opt_index >= len(q['options']):
                    return
                session['answers'][client_id] = opt_index
            push(client_id, {'type': 'answer-received', 'optionIndex': opt_index})
            push_trainers(room_id, {
                'type': 'stats-update',
                'stats': compute_stats(room_id),
                'students': get_student_list(room_id),
            })

        # ── Formation management ──────────────────────────────────────
        elif t == 'trainer-select-formation':
            if role != 'trainer':
                return
            formation_name = msg.get('formation', '')
            with state_lock:
                trainer_formations = all_data['formations'].get(room_id, {})
                if formation_name not in trainer_formations:
                    push(client_id, {'type': 'error', 'message': 'Formation introuvable'})
                    return
                room['formation'] = formation_name
                room['questions'] = trainer_formations[formation_name]
                room['session'] = {'phase': 'idle', 'currentIndex': -1, 'answers': {}}
            push_trainers(room_id, {
                'type': 'formation-selected',
                'formation': formation_name,
                'questions': room['questions'],
            })
            push_students(room_id, {'type': 'session-reset'})

        elif t == 'trainer-create-formation':
            if role != 'trainer':
                return
            formation_name = msg.get('formation', '').strip()
            if not formation_name:
                return
            with state_lock:
                if room_id not in all_data['formations']:
                    all_data['formations'][room_id] = {}
                if formation_name in all_data['formations'][room_id]:
                    push(client_id, {'type': 'error', 'message': 'Cette formation existe deja'})
                    return
                all_data['formations'][room_id][formation_name] = []
                save_data()
                formations = list(all_data['formations'][room_id].keys())
            push_trainers(room_id, {'type': 'formations-updated', 'formations': formations})

        elif t == 'trainer-delete-formation':
            if role != 'trainer':
                return
            formation_name = msg.get('formation', '').strip()
            with state_lock:
                trainer_formations = all_data['formations'].get(room_id, {})
                if formation_name not in trainer_formations:
                    return
                del trainer_formations[formation_name]
                save_data()
                if room['formation'] == formation_name:
                    room['formation'] = None
                    room['questions'] = []
                    room['session'] = {'phase': 'idle', 'currentIndex': -1, 'answers': {}}
                formations = list(trainer_formations.keys())
            push_trainers(room_id, {
                'type': 'formations-updated',
                'formations': formations,
                'currentFormation': room['formation'],
            })
            if room['formation'] is None:
                push_students(room_id, {'type': 'session-reset'})

        elif t == 'trainer-set-questions':
            if role != 'trainer':
                return
            qs = msg.get('questions', [])
            with state_lock:
                formation_name = room['formation']
                if not formation_name:
                    return
                room['questions'] = qs
                all_data['formations'][room_id][formation_name] = qs
                save_data()
            push(client_id, {'type': 'questions-updated', 'questions': qs})

        elif t == 'trainer-import':
            if role != 'trainer':
                return
            import_data = msg.get('importData', {})
            imported_formations = import_data.get('formations', {})
            if not isinstance(imported_formations, dict):
                return
            with state_lock:
                if room_id not in all_data['formations']:
                    all_data['formations'][room_id] = {}
                for fname, fquestions in imported_formations.items():
                    if isinstance(fquestions, list):
                        all_data['formations'][room_id][fname] = fquestions
                save_data()
                formations = list(all_data['formations'][room_id].keys())
            push_trainers(room_id, {'type': 'formations-updated', 'formations': formations})

        # ── Quiz session controls ─────────────────────────────────────
        elif t == 'trainer-launch':
            if role != 'trainer':
                return
            try:
                q_index = int(msg.get('questionIndex', -1))
            except (TypeError, ValueError):
                return
            with state_lock:
                questions = room['questions']
                if q_index < 0 or q_index >= len(questions):
                    return
                room['session']['currentIndex'] = q_index
                room['session']['phase'] = 'active'
                room['session']['answers'] = {}
                q = questions[q_index]
            push_students(room_id, {
                'type': 'question-active',
                'question': {
                    'index': q_index,
                    'total': len(questions),
                    'text': q['text'],
                    'options': q['options'],
                },
            })
            push_trainers(room_id, {
                'type': 'question-launched',
                'questionIndex': q_index,
                'stats': compute_stats(room_id),
                'students': get_student_list(room_id),
            })

        elif t == 'trainer-close':
            if role != 'trainer':
                return
            with state_lock:
                room['session']['phase'] = 'closed'
            push_students(room_id, {'type': 'answers-closed'})
            push_trainers(room_id, {'type': 'question-closed', 'stats': compute_stats(room_id)})

        elif t == 'trainer-reveal':
            if role != 'trainer':
                return
            with state_lock:
                session = room['session']
                questions = room['questions']
                if session['currentIndex'] < 0 or session['currentIndex'] >= len(questions):
                    return
                session['phase'] = 'revealed'
                correct_idx = questions[session['currentIndex']]['correctIndex']
            push_all(room_id, {
                'type': 'answer-revealed',
                'correctIndex': correct_idx,
                'stats': compute_stats(room_id),
            })

        elif t == 'trainer-reset':
            if role != 'trainer':
                return
            with state_lock:
                room['session'] = {'phase': 'idle', 'currentIndex': -1, 'answers': {}}
            push_all(room_id, {'type': 'session-reset'})
            push_trainers(room_id, {'type': 'students-update', 'students': get_student_list(room_id)})


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Quiz Formateur en ligne sur le port {PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print('Serveur arrete.')
