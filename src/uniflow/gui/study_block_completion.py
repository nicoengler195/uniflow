"""Calendar-owned explicit completion dialog using the B2 lifecycle guards."""
from dataclasses import asdict
import json
from PySide6.QtWidgets import QLabel
from uuid import uuid4
from uniflow.gui.study_block_changes import StudyBlockChangeDialog
from uniflow.application.study_blocks import canonical
from uniflow.application.study_block_changes import list_study_block_states, study_block_history
from uniflow.application.study_block_completion import (preview_study_block_completion,
    confirm_study_block_completion,project_study_block_progress)


class StudyBlockCompletionDialog(StudyBlockChangeDialog):
    def __init__(self,*args,**kwargs):
        parent=kwargs.get('parent',args[3] if len(args)>3 else None)
        self.course_names=dict(getattr(getattr(parent,'projection',None),'courses',()))
        super().__init__(*args,**kwargs)
        for label in self.findChildren(QLabel):
            if label.text().startswith('Bestätigte und abgesagte'):label.setText('Lernblöcke · Abschluss und Nachweise · '+self.semester_id+' · '+self.zone.key)
            if label.text().startswith('Lernzieltext'):label.hide()
        self.setWindowTitle('Lernblock abschließen · Abschlussnachweis')
        for w in (self.day,self.begin,self.minutes,self.title,self.cancel,self.cancel_ack):w.hide()
        self.check.setText('Verbindliche Abschlussvorschau anzeigen')
        self.ack.setText('Ich bestätige ausdrücklich, dass ich die angezeigte Lernaktivität abgeschlossen habe.')
        self.apply.setText('Als abgeschlossen markieren')
        self.status.setText('Ohne Session-Verknüpfung: Benutzerangabe (USER_REPORTED), kein unabhängiger Leistungsnachweis. Keine Mastery- oder Scoringänderung. Eine verlässliche Block-/Session-Zuordnung ist im bestehenden Vertrag nicht vorhanden.')

    def update_apply(self,*args):
        self.apply.setEnabled(bool(self.preview and self.ack.isChecked() and not self.busy and self.context_current()))

    def load_states(self):
        selected=self.blocks.currentData() or self.initial_block
        def read():
            states=list_study_block_states(self.controller.config,self.semester_id)
            progress=project_study_block_progress(self.controller.config,self.semester_id)
            histories={b['id']:study_block_history(self.controller.config,self.semester_id,b['id']) for b in states}
            return states,histories,progress
        def show(result):
            self.progress={p.block_id:p for p in result[2]}
            self.show_states(result[:2],selected)
        self.run('study_completion_history',read,show)

    def select_block(self,*args):
        super().select_block(*args)
        if not self.current:return
        p=getattr(self,'progress',{}).get(self.current['id'])
        if p and p.completion:
            e=p.completion
            self.history.appendPlainText(f'\nAbschlussnachweis · {e.evidence_type}\nBestätigt: {e.confirmed_at} · {e.actor}\nHerkunft: {e.origin} · Revision {e.expected_revision}\nCompletion-ID: {e.id}\nRequest-ID: {e.request_id}\nKeine Session verknüpft · Keine Mastery-Bewertung\n'+canonical(asdict(e)))
        self.check.setEnabled(self.current['status']=='CONFIRMED' and not self.busy and self.context_current())

    def load_preview(self):
        if not self.current or self.current['status']!='CONFIRMED':return
        b=self.current;self.invalidate();self.request_id=str(uuid4())
        self.run('study_completion_preview',lambda:preview_study_block_completion(self.controller.config,self.semester_id,b['id'],b['revision']),self.show_preview)

    def show_preview(self,p):
        if p.semester_id!=self.semester_id or not self.context_current():return
        self.preview=p
        self.comparison.setPlainText(self.describe('Abzuschließender Lernblock',p.state_json)+'\n\nNeuer Status: COMPLETED\nEvidenz: USER_REPORTED · keine Session verknüpft.\nBenutzerangabe, kein unabhängiger Leistungsnachweis. Keine Mastery- oder Scoringänderung.')
        self.update_apply()

    def commit(self):
        if not self.context_current() or not self.preview or not self.ack.isChecked():return
        p=self.preview;request=self.request_id
        self.run('study_completion_confirm',lambda:confirm_study_block_completion(self.controller.config,p,request_id=request,confirmed=True,actor='local-user',expected_fingerprint=p.fingerprint),self.applied)

    def applied(self,evidence):
        self.invalidate();self.status.setText(f'COMPLETED gespeichert · {evidence.evidence_type} · {evidence.confirmed_at} · Request-ID: {evidence.request_id}')
        if self.parent() is not None and hasattr(self.parent(),'reload'):
            self.parent().reload();self.parent_generation=self.parent().generation
        self.load_states()

    def describe(self,label,state):
        result=super().describe(label,state)
        course_id=json.loads(state)['course_id']
        return result.replace('Kurs: '+course_id,'Kurs: '+self.course_names.get(course_id,course_id))
