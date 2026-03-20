#!/usr/bin/env python3
"""
Quiz Formateur — Serveur en ligne
Aucune dépendance externe requise (Python 3.7+ seulement)
Prêt pour déploiement sur Render / Fly.io / Railway.
"""

import json
import queue
import threading
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT             = int(os.environ.get('PORT', 3000))
TRAINER_PASSWORD = os.environ.get('TRAINER_PASSWORD', 'formateur2025')
PUBLIC_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public')

# ── Question persistence ──────────────────────────────────────────────────
QUESTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'questions.json')


def load_questions():
    try:
        with open(QUESTIONS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def save_questions(questions):
    with open(QUESTIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(questions, f, ensure_ascii=False, indent=2)


# ── Server state ───────────────────────────────────────────────────────────
state_lock    = threading.RLock()
question_bank = load_questions()
session       = {'phase': 'idle', 'currentIndex': -1, 'answers': {}}
clients       = {}


def compute_stats():
    with state_lock:
        if session['currentIndex'] < 0:
            return None
        q        = question_bank[session['currentIndex']]
        counts   = [0] * len(q['options'])
        for idx in session['answers'].values():
            if 0 <= idx < len(counts):
                counts[idx] += 1
        answered = len(session['answers'])
        total    = sum(1 for c in clients.values() if c['role'] == 'student')
        percs    = [round(c / answered * 100) if answered > 0 else 0 for c in counts]
        return {
            'totalStudents':   total,
            'answeredCount':   answered,
            'answeredPercent': round(answered / total * 100) if total > 0 else 0,
            'counts':          counts,
            'percentages':     percs,
            'correctIndex':    q['correctIndex'] if session['phase'] == 'revealed' else None,
        }


def get_student_list():
    with state_lock:
        return [
            {'id': cid, 'name': c['name'], 'answered': cid in session['answers']}
            for cid, c in clients.items()
            if c['role'] == 'student'
        ]


def _enqueue(c, event):
    try:
        c['queue'].put_nowait(event)
    except queue.Full:
        pass


def push(client_id, event):
    with state_lock:
        if client_id in clients:
            _enqueue(clients[client_id], event)


def push_to_trainers(event):
    with state_lock:
        for c in clients.values():
            if c['role'] == 'trainer':
                _enqueue(c, event)


def push_to_students(event):
    with state_lock:
        for c in clients.values():
            if c['role'] == 'student':
                _enqueue(c, event)


def push_to_all(event):
    with state_lock:
        for c in clients.values():
            _enqueue(c, event)


# ── HTTP Handler ───────────────────────────────────────────────────────────
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
        path   = parsed.path.rstrip('/') or '/'
        params = parse_qs(parsed.query)

        if path in ('/', '/student'):
            self._serve_file('student.html')
        elif path == '/trainer':
            self._serve_file('trainer.html')
        elif path == '/events':
            self._handle_sse(params)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
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
        client_id = (params.get('clientId') or [None])[0]
        role      = (params.get('role')     or [''])[0]
        name      = (params.get('name')     or [''])[0].strip()[:25]
        password  = (params.get('password') or [''])[0]

        if not client_id:
            self.send_response(400)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header('Content-Type',      'text/event-stream')
        self.send_header('Cache-Control',     'no-cache')
        self.send_header('Connection',        'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self._cors()
        self.end_headers()

        q = queue.Queue(maxsize=200)

        if role == 'trainer':
            if password != TRAINER_PASSWORD:
                self._sse({'type': 'auth-failed'})
                return
            with state_lock:
                clients[client_id] = {'queue': q, 'role': 'trainer', 'name': None}
            self._sse({
                'type':       'trainer-ok',
                'studentUrl': '',
                'questions':  question_bank,
                'session':    session,
                'students':   get_student_list(),
                'stats':      compute_stats(),
            })

        elif role == 'student':
            if not name:
                self._sse({'type': 'error', 'message': 'Nom invalide'})
                return

            with state_lock:
                taken = any(
                    c['role'] == 'student'
                    and (c.get('name') or '').lower() == name.lower()
                    and cid != client_id
                    for cid, c in clients.items()
                )
                if taken:
                    self._sse({'type': 'name-taken'})
                    return

                clients[client_id] = {'queue': q, 'role': 'student', 'name': name}

                phase       = session['phase']
                my_answer   = session['answers'].get(client_id)
                correct_idx = None
                current_q   = None
                if session['currentIndex'] >= 0 and phase != 'idle':
                    sq = question_bank[session['currentIndex']]
                    current_q = {
                        'index':   session['currentIndex'],
                        'total':   len(question_bank),
                        'text':    sq['text'],
                        'options': sq['options'],
                    }
                    if phase == 'revealed':
                        correct_idx = sq['correctIndex']

            self._sse({
                'type':            'student-ok',
                'name':            name,
                'phase':           phase,
                'currentQuestion': current_q,
                'myAnswer':        my_answer,
                'correctIndex':    correct_idx,
            })
            push_to_trainers({'type': 'students-update', 'students': get_student_list()})

        else:
            self._sse({'type': 'error', 'message': 'Rôle invalide'})
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
                was_student = clients.get(client_id, {}).get('role') == 'student'
                clients.pop(client_id, None)
            if was_student:
                push_to_trainers({'type': 'students-update', 'students': get_student_list()})

    def _sse(self, data):
        line = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
        self.wfile.write(line.encode('utf-8'))
        self.wfile.flush()

    def _handle_action(self, msg):
        t         = msg.get('type')
        client_id = msg.get('clientId')
        if not client_id:
            return

        with state_lock:
            role = clients.get(client_id, {}).get('role')

        if t == 'student-answer':
            if role != 'student':
                return
            with state_lock:
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
                q = question_bank[session['currentIndex']]
                if opt_index < 0 or opt_index >= len(q['options']):
                    return
                session['answers'][client_id] = opt_index
            push(client_id, {'type': 'answer-received', 'optionIndex': opt_index})
            push_to_trainers({
                'type':     'stats-update',
                'stats':    compute_stats(),
                'students': get_student_list(),
            })

        elif t == 'trainer-set-questions':
            if role != 'trainer':
                return
            qs = msg.get('questions', [])
            with state_lock:
                question_bank.clear()
                question_bank.extend(qs)
            save_questions(question_bank)
            push(client_id, {'type': 'questions-updated', 'questions': question_bank})

        elif t == 'trainer-launch':
            if role != 'trainer':
                return
            try:
                q_index = int(msg.get('questionIndex', -1))
            except (TypeError, ValueError):
                return
            with state_lock:
                if q_index < 0 or q_index >= len(question_bank):
                    return
                session['currentIndex'] = q_index
                session['phase']        = 'active'
                session['answers']      = {}
                q = question_bank[q_index]
            push_to_students({
                'type': 'question-active',
                'question': {
                    'index':   q_index,
                    'total':   len(question_bank),
                    'text':    q['text'],
                    'options': q['options'],
                },
            })
            push_to_trainers({
                'type':          'question-launched',
                'questionIndex': q_index,
                'stats':         compute_stats(),
                'students':      get_student_list(),
            })

        elif t == 'trainer-close':
            if role != 'trainer':
                return
            with state_lock:
                session['phase'] = 'closed'
            push_to_students({'type': 'answers-closed'})
            push_to_trainers({'type': 'question-closed', 'stats': compute_stats()})

        elif t == 'trainer-reveal':
            if role != 'trainer':
                return
            with state_lock:
                if session['currentIndex'] < 0:
                    return
                session['phase'] = 'revealed'
                correct_idx = question_bank[session['currentIndex']]['correctIndex']
            push_to_all({
                'type':         'answer-revealed',
                'correctIndex': correct_idx,
                'stats':        compute_stats(),
            })

        elif t == 'trainer-reset':
            if role != 'trainer':
                return
            with state_lock:
                session['phase']        = 'idle'
                session['currentIndex'] = -1
                session['answers']      = {}
            push_to_all({'type': 'session-reset'})
            push_to_trainers({'type': 'students-update', 'students': get_student_list()})


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Quiz Formateur en ligne sur le port {PORT}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print('Serveur arrete.')
