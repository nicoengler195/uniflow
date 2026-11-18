"""Explicit availability and proposal confirmation, using existing TaskRunner."""
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo
from PySide6.QtCore import QDate, QTime, Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QDateEdit,QTimeEdit,QCheckBox,QTableWidget,QTableWidgetItem,QSpinBox,QAbstractItemView)
from uniflow.application.semester_read_models import list_semesters
from uniflow.application.study_blocks import confirm_availability, preview_study_blocks, confirm_study_blocks, request_status, selection_fingerprint


class StudyBlockConfirmationDialog(QDialog):
    def __init__(self, controller, projection, parent=None):
        super().__init__(parent)
        self.setObjectName("studyBlockDialog")
        self.controller,self.projection=controller,projection
        semester=next((s for s in list_semesters(controller.config) if s.stable_id==projection.semester_id),None)
        if semester is None:
            raise ValueError('UNKNOWN_SEMESTER')
        self.semester_id=semester.stable_id
        self.semester_zone=ZoneInfo(semester.timezone)
        self.preview=None; self.request_id=str(uuid4()); self.busy=False; self.token=0
        self.setWindowTitle('Lernblöcke prüfen und ausdrücklich bestätigen'); self.resize(1100,820)
        layout=QVBoxLayout(self)
        layout.addWidget(QLabel('Freigegebene Lernzeit · konkreter Tag · '+semester.timezone))
        line=QHBoxLayout(); self.day=QDateEdit(QDate(projection.as_of.astimezone(self.semester_zone).date())); self.day.setCalendarPopup(True)
        self.begin=QTimeEdit(QTime(9,0)); self.end=QTimeEdit(QTime(10,0))
        self.availability_ack=QCheckBox('Dieses Zeitfenster ausdrücklich freigeben')
        self.save_window=QPushButton('Verfügbarkeit bestätigen')
        for w in (self.day,self.begin,self.end,self.availability_ack,self.save_window):line.addWidget(w)
        layout.addLayout(line)
        layout.addWidget(QLabel('Dauer pro Lernbedarf auswählen (0 = keine Dauer angegeben)'))
        self.durations=QTableWidget(len(projection.needs),2); self.durations.setHorizontalHeaderLabels(['Lernziel','Minuten'])
        self.spins={}
        for row,n in enumerate(projection.needs):
            item=QTableWidgetItem(n.course_name+' · '+n.action); item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.durations.setItem(row,0,item); spin=QSpinBox(); spin.setRange(0,1440); spin.setValue(n.known_minutes or 0)
            self.durations.setCellWidget(row,1,spin);self.spins[n.recommendation_id]=spin
            spin.valueChanged.connect(self.invalidate)
        self.durations.horizontalHeader().setStretchLastSection(True);self.durations.setColumnWidth(0,740);self.durations.setMaximumHeight(190);layout.addWidget(self.durations)
        self.check=QPushButton('Zeitvorschläge und Konflikte prüfen');layout.addWidget(self.check)
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['Übernehmen / Lernziel','Anfang','Ende'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setColumnWidth(0,650);self.table.setColumnWidth(1,180);self.table.setColumnWidth(2,180);layout.addWidget(self.table)
        self.summary=QLabel('Noch keine geprüfte Vorschau.');self.summary.setWordWrap(True);self.summary.setMinimumHeight(90);layout.addWidget(self.summary)
        self.ack=QCheckBox('Ich bestätige die ausgewählten Lernblöcke zu den angezeigten Zeiten.');layout.addWidget(self.ack)
        self.apply=QPushButton('Ausgewählte Lernblöcke gemeinsam bestätigen');self.apply.setEnabled(False);layout.addWidget(self.apply)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setMinimumHeight(40);layout.addWidget(self.status)
        self.recover=QPushButton('Persistenten Request-Status erneut lesen');layout.addWidget(self.recover)
        self.check.clicked.connect(self.load_preview);self.save_window.clicked.connect(self.save_availability)
        self.apply.clicked.connect(self.commit);self.ack.toggled.connect(self.update_summary);self.table.itemChanged.connect(self.selection_changed)
        self.recover.clicked.connect(self.read_status)

    def invalidate(self):
        self.preview=None;self.ack.setChecked(False);self.apply.setEnabled(False)
        self.table.setRowCount(0);self.summary.setText('Eingaben geändert; Vorschau erneut prüfen.')

    def context_current(self):
        parent=self.parent()
        if parent is not None and hasattr(parent,'projection') and hasattr(parent,'semester'):
            if parent.projection is not self.projection or parent.semester.currentData()!=self.semester_id:
                self.invalidate();self.status.setText('Semester gewechselt; Lernprioritäten erneut laden.')
                return False
        return True

    def run(self, name, work, done):
        if self.busy or not self.context_current():return
        self.busy=True;self.token+=1;token=self.token
        for w in (self.save_window,self.check,self.apply,self.durations,self.table,self.ack,self.recover):w.setEnabled(False)
        def completed(operation,generation,result):
            if generation != self.token:return
            if not self.context_current():
                self.busy=False
                return
            self.busy=False
            for w in (self.save_window,self.check,self.durations,self.table,self.ack,self.recover):w.setEnabled(True)
            if hasattr(result,'message'):
                self.status.setText(f'{result.message} · Request-ID: {self.request_id}')
                self.apply.setEnabled(False)
            else:done(result)
        self.controller.runner.submit(name,token,work,completed)

    def save_availability(self):
        if not self.availability_ack.isChecked():
            self.status.setText('Bitte die Verfügbarkeit ausdrücklich freigeben.');return
        day,begin,end=self.day.date().toPython(),self.begin.time().toPython(),self.end.time().toPython()
        self.invalidate()
        self.run('study_availability',lambda:confirm_availability(self.controller.config,self.semester_id,day,begin,end,
            confirmed=True,actor='local-user',as_of=datetime.now(ZoneInfo('UTC'))),self.saved_availability)

    def saved_availability(self,result):
        self.availability_ack.setChecked(False);self.status.setText('Verfügbarkeit bestätigt: '+result)

    def load_preview(self):
        durations={k:s.value() for k,s in self.spins.items() if s.value()>0}
        self.invalidate();self.request_id=str(uuid4())
        self.run('study_preview',lambda:preview_study_blocks(self.controller.config,self.semester_id,
            self.projection.as_of,durations,self.projection.horizon_days),self.show_preview)

    def show_preview(self,preview):
        if not self.context_current():return
        if preview.semester_id!=self.semester_id:
            self.invalidate();self.status.setText('Vorschau gehört zu einem anderen Semester.');return
        self.preview=preview;self.table.blockSignals(True);self.table.setRowCount(len(preview.proposals))
        for row,p in enumerate(preview.proposals):
            name=next(n.course_name for n in self.projection.needs if n.recommendation_id==p.need_id)
            title=QTableWidgetItem(name+' · '+p.title);title.setFlags(title.flags()|Qt.ItemFlag.ItemIsUserCheckable);title.setCheckState(Qt.CheckState.Checked)
            title.setData(Qt.ItemDataRole.UserRole,p.need_id);self.table.setItem(row,0,title)
            for col,value in ((1,p.starts_at),(2,p.ends_at)):
                item=QTableWidgetItem(datetime.fromisoformat(value).astimezone(self.semester_zone).strftime('%d.%m.%Y %H:%M'))
                item.setToolTip(value);self.table.setItem(row,col,item)
        self.table.blockSignals(False);self.status.setText('\n'.join(preview.diagnostics));self.update_summary()

    def selected(self):
        return [self.table.item(r,0).data(Qt.ItemDataRole.UserRole) for r in range(self.table.rowCount()) if self.table.item(r,0).checkState()==Qt.CheckState.Checked]

    def selection_changed(self,*args):
        self.ack.setChecked(False)
        self.update_summary()

    def update_summary(self,*args):
        selected=self.selected();minutes=sum(p.minutes for p in self.preview.proposals if p.need_id in selected) if self.preview else 0
        self.summary.setText(f'Bisherige bestätigte Blöcke aus dieser Vorschlagsauswahl: 0\nVorgeschlagene neue Blöcke: {len(selected)}\nZusätzliche bestätigte Lernzeit: {minutes} Minuten\nVorhandene Semestertermine bleiben unverändert.')
        self.apply.setEnabled(bool(self.preview and selected and self.ack.isChecked() and not self.busy))

    def commit(self):
        if not self.preview or not self.ack.isChecked():return
        preview,selected,request=self.preview,self.selected(),self.request_id
        expected=selection_fingerprint(preview,selected)
        self.run('study_confirm',lambda:confirm_study_blocks(self.controller.config,preview,selected,
            request_id=request,confirmed=True,actor='local-user',expected_selection_fingerprint=expected),self.applied)

    def applied(self,result):
        self.invalidate();self.status.setText(f'{len(result)} Lernblöcke bestätigt und gespeichert. Request-ID: {self.request_id}')

    def read_status(self):
        request=self.request_id
        self.run('study_request_status',lambda:request_status(self.controller.config,request),
            lambda result:self.status.setText('Request bestätigt: '+result['result_json'] if result else 'Kein persistenter Request-Nachweis vorhanden.'))

    def reject(self):
        if not self.busy:super().reject()
