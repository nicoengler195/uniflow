"""Calendar-owned B2 editor; all persistence uses the existing TaskRunner."""
from datetime import datetime
import json
from uuid import uuid4
from zoneinfo import ZoneInfo
from PySide6.QtCore import QDate,QTime
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,
    QDateEdit,QTimeEdit,QSpinBox,QLineEdit,QCheckBox,QPushButton,QPlainTextEdit)
from uniflow.application.study_block_changes import (list_study_block_states,study_block_history,
    StudyBlockChangeCommandV1,preview_study_block_change,confirm_study_block_change)


class StudyBlockChangeDialog(QDialog):
    def __init__(self,controller,semester_id,timezone,parent=None,block_id=None):
        super().__init__(parent)
        self.setObjectName("studyBlockDialog")
        self.controller=controller;self.semester_id=semester_id;self.zone=ZoneInfo(timezone)
        self.invalidated=False;self.parent_generation=getattr(parent,'generation',None)
        self.initial_block=block_id;self.busy=False;self.token=0;self.closed=False
        self.preview=None;self.current=None;self.states=();self.request_id=str(uuid4())
        self.setWindowTitle('Lernblock ändern · Historie');self.resize(1050,850)
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel('Bestätigte und abgesagte Lernblöcke · '+semester_id+' · '+timezone))
        self.blocks=QComboBox();layout.addWidget(self.blocks)
        line=QHBoxLayout();self.day=QDateEdit();self.day.setCalendarPopup(True);self.begin=QTimeEdit()
        self.minutes=QSpinBox();self.minutes.setRange(1,1440)
        for w in (self.day,self.begin,self.minutes):line.addWidget(w)
        layout.addLayout(line)
        layout.addWidget(QLabel('Lernzieltext (Kurs- und Skillidentität bleiben erhalten)'))
        self.title=QLineEdit();self.title.setMaxLength(2000);layout.addWidget(self.title)
        self.cancel=QCheckBox('Diesen Lernblock ausdrücklich absagen');layout.addWidget(self.cancel)
        self.check=QPushButton('Vorher / Nachher und Konflikte prüfen');layout.addWidget(self.check)
        self.comparison=QPlainTextEdit();self.comparison.setReadOnly(True);layout.addWidget(self.comparison)
        self.ack=QCheckBox('Ich bestätige genau die angezeigte Änderung.');layout.addWidget(self.ack)
        self.cancel_ack=QCheckBox('Ich bestätige die Absage separat.');layout.addWidget(self.cancel_ack)
        self.apply=QPushButton('Neue Revision verbindlich speichern');layout.addWidget(self.apply)
        self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        self.history=QPlainTextEdit();self.history.setReadOnly(True);layout.addWidget(self.history)
        self.refresh=QPushButton('Historie aus Datenbank erneut lesen');layout.addWidget(self.refresh)
        self.blocks.currentIndexChanged.connect(self.select_block)
        for signal in (self.day.dateChanged,self.begin.timeChanged,self.minutes.valueChanged,self.title.textChanged,self.cancel.toggled):signal.connect(self.invalidate)
        self.cancel.toggled.connect(self.cancel_mode)
        self.ack.toggled.connect(self.update_apply);self.cancel_ack.toggled.connect(self.update_apply)
        self.check.clicked.connect(self.load_preview);self.apply.clicked.connect(self.commit);self.refresh.clicked.connect(self.load_states)
        if parent is not None and hasattr(parent,'semester'):
            parent.semester.currentIndexChanged.connect(self.semester_changed)
        self.update_apply();self.load_states()

    def semester_changed(self,*args):
        self.invalidated=True;self.token+=1
        self.invalidate()
        self.set_controls(False)
        self.status.setText('Semester gewechselt; Dialog erneut öffnen.')

    def context_current(self):
        parent=self.parent()
        return not self.closed and not self.invalidated and (parent is None or not hasattr(parent,'semester') or
            (parent.semester.currentData()==self.semester_id and parent.active and parent.generation==self.parent_generation))

    def run(self,name,work,done):
        if self.busy or not self.context_current():return
        self.busy=True;self.token+=1;token=self.token;self.set_controls(False)
        def completed(operation,generation,result):
            if generation!=self.token or self.closed:
                if self.invalidated:self.busy=False
                return
            self.busy=False
            if not self.context_current():
                self.invalidated=True;self.invalidate();self.set_controls(False);self.status.setText('Semester gewechselt; Dialog erneut öffnen.');return
            self.set_controls(True)
            if hasattr(result,'message'):
                self.invalidate();self.status.setText(result.message+' · Request-ID: '+self.request_id)
            else:done(result)
            self.update_apply()
        self.controller.runner.submit(name,token,work,completed)

    def set_controls(self,enabled):
        for w in (self.blocks,self.day,self.begin,self.minutes,self.title,self.cancel,self.check,self.ack,self.cancel_ack,self.refresh):w.setEnabled(enabled)
        self.apply.setEnabled(False)
        if enabled:self.cancel_mode()

    def invalidate(self,*args):
        self.preview=None;self.ack.setChecked(False);self.cancel_ack.setChecked(False)
        self.comparison.clear();self.update_apply()

    def cancel_mode(self,*args):
        for w in (self.day,self.begin,self.minutes,self.title):w.setEnabled(not self.busy and not self.cancel.isChecked())
        self.cancel_ack.setVisible(self.cancel.isChecked())

    def update_apply(self,*args):
        self.apply.setEnabled(bool(self.preview and self.ack.isChecked() and
            (not self.preview.command.cancel or self.cancel_ack.isChecked()) and not self.busy and self.context_current()))

    def load_states(self):
        selected=self.blocks.currentData() or self.initial_block
        def read():
            states=list_study_block_states(self.controller.config,self.semester_id)
            return states,{b['id']:study_block_history(self.controller.config,self.semester_id,b['id']) for b in states}
        self.run('study_change_history',read,lambda result:self.show_states(result,selected))

    def show_states(self,result,selected):
        self.states,self.histories=result
        self.blocks.blockSignals(True);self.blocks.clear()
        for b in self.states:self.blocks.addItem(f"{b['title']} · {b['status']} · Revision {b['revision']}",b['id'])
        self.blocks.setCurrentIndex(max(0,self.blocks.findData(selected)));self.blocks.blockSignals(False)
        self.select_block()

    def select_block(self,*args):
        self.invalidate();self.current=next((b for b in self.states if b['id']==self.blocks.currentData()),None)
        if not self.current:return
        b=self.current;start=datetime.fromisoformat(b['starts_at']).astimezone(self.zone)
        self.day.setDate(QDate(start.date()));self.begin.setTime(QTime(start.time()));self.minutes.setValue(b['minutes']);self.title.setText(b['title']);self.cancel.setChecked(False)
        self.history.setPlainText('\n\n'.join(self.describe('Historie · '+r.change_kind,r.state_json)+f'\nBestätigt: {r.confirmed_at} · {r.confirmed_by}\nRequest: {r.request_id}' for r in self.histories[b['id']]))
        self.check.setEnabled(b['status']=='CONFIRMED');self.cancel.setEnabled(b['status']=='CONFIRMED')

    def load_preview(self):
        if not self.current or self.current['status']!='CONFIRMED':return
        b=self.current
        if self.cancel.isChecked():
            start=datetime.fromisoformat(b['starts_at']).astimezone(self.zone)
            day,begin,minutes,title=start.date(),start.time(),b['minutes'],b['title']
        else:day,begin,minutes,title=self.day.date().toPython(),self.begin.time().toPython(),self.minutes.value(),self.title.text()
        command=StudyBlockChangeCommandV1(b['id'],b['revision'],day,begin,minutes,title,self.cancel.isChecked())
        self.invalidate();self.request_id=str(uuid4())
        self.run('study_change_preview',lambda:preview_study_block_change(self.controller.config,self.semester_id,command),self.show_preview)

    def show_preview(self,p):
        if p.semester_id!=self.semester_id or not self.context_current():return
        self.preview=p
        self.comparison.setPlainText(self.describe('Vorher',p.before_json)+'\n\n'+self.describe('Nachher',p.after_json)+'\n\n'+'\n'.join(p.diagnostics));self.update_apply()

    def describe(self,label,state):
        b=json.loads(state)
        start=datetime.fromisoformat(b['starts_at']).astimezone(self.zone)
        end=datetime.fromisoformat(b['ends_at']).astimezone(self.zone)
        courses=dict(getattr(getattr(self.parent(),'projection',None),'courses',()))
        course=courses.get(b['course_id'],b['course_id'])
        skills=', '.join(json.loads(b['skill_ids_json'])) or 'keine spezifische Skillzuordnung'
        return f"{label} · Revision {b['revision']} · {b['status']}\n{start:%d.%m.%Y %H:%M} – {end:%H:%M} · {b['minutes']} Minuten\nKurs: {course} · Skills: {skills}\nLernziel: {b['title']}"

    def commit(self):
        if not self.context_current() or not self.preview or not self.ack.isChecked() or (self.preview.command.cancel and not self.cancel_ack.isChecked()):return
        p=self.preview;request=self.request_id
        self.run('study_change_confirm',lambda:confirm_study_block_change(self.controller.config,p,request_id=request,confirmed=True,actor='local-user',expected_fingerprint=p.fingerprint),self.applied)

    def applied(self,receipt):
        self.invalidate();self.status.setText(f'Revision {receipt.revision} gespeichert · {receipt.status} · Request-ID: {receipt.request_id}')
        if self.parent() is not None and hasattr(self.parent(),'reload'):
            self.parent().reload()
            self.parent_generation=self.parent().generation
        self.load_states()

    def reject(self):
        if not self.busy:
            self.closed=True;self.token+=1;super().reject()
