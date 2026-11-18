"""Thin orchestration boundary between Qt and application services."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from PySide6.QtCore import QObject, Signal

from uniflow.application.application_read_models import get_home_dashboard
from uniflow.application import planning_workflow, planning_calendar
from uniflow.application.week_read_models import get_week_view
from uniflow.application.session_inbox_read_models import get_session_inbox
from uniflow.application.session_detail_read_models import get_session_detail
from uniflow.application.materials_context_read_models import get_materials_context
from uniflow.application.material_inbox_read_models import get_material_inbox
from uniflow.application.material_detail_read_models import get_material_detail
from uniflow.application.dashboard_projection import project_dashboard
from uniflow.application import learning_workflow
from uniflow.application import gui_learning_bridge
from uniflow.application import review_workflow
from uniflow.application.review_read_models import get_classification_review, get_skill_review
from uniflow.application.skill_choice_read_models import search_active_skills
from uniflow.gui.presentation import PresentationError
from uniflow.gui.routes import Route
from uniflow.gui.state import AppState, Busy, ReviewFamily
from uniflow.gui.workers import TaskRunner
from uniflow.schemas.planning_workflow_v1 import (
    AcceptPlanRequestV1, CreatePlanRequestV1, MaterializeBlockRequestV1,
    MoveBlockRequestV1, PlaceUnscheduledRequestV1, RegeneratePlanRequestV1,
    RemoveBlockRequestV1, SetBlockDurationRequestV1,
)
from uniflow.schemas.workflow_v1 import (ApplyMasteryRequestV1, CreateSessionRequestV1, ExportExchangeRequestV1,
    GenerateHandoffRequestV1, ImportResultFileRequestV1, StartSessionRequestV1)
from uniflow.schemas.review_workflow_v1 import (
    ClassificationAcceptRequestV1, ClassificationRejectRequestV1, SkillAcceptNewRequestV1,
    SkillResolveExistingRequestV1, SkillRejectRequestV1)


class GuiController(QObject):
    home_changed = Signal(object)
    home_loading = Signal()
    dashboard_changed = Signal(object)
    week_changed = Signal(object)
    sessions_changed = Signal(object)
    session_detail_changed = Signal(object)
    materials_changed = Signal(object)
    material_detail_changed = Signal(object)
    proposal_review_changed = Signal(object)
    skill_choices_changed = Signal(object)
    review_opened = Signal()
    state_changed = Signal()
    feedback = Signal(object)

    def __init__(self, config, semester, *, runner=None, clock=None):
        super().__init__()
        self.config = config
        self.semester = semester
        self.state = AppState(config.project_root, semester.stable_id)
        self.runner = runner or TaskRunner()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.home = None
        self.dashboard_projection = None
        self._closed = False
        self.weekly_drops = None
        self.week = None
        self.sessions = None
        self.session_detail = None
        self.materials_context = None
        self.materials = None
        self.material_detail = None
        self.proposal_review = None
        self.skill_choices = None
        self.selected_review_skill_id = None
        self.review_mutation_busy = False
        self._review_readback_identity = None
        self.materials_as_of = None
        self.learning_mutation_busy = False
        self.bridge_busy = False
        self._navigation_epoch = 0
        self._selection_generation = 0
        self._import_origin = None
        self._pending_import_material = None
        self._pending_proposal_review = None
        self._bridge_generation = 0
        self._generation = {"home": 0, "week": 0, "sessions": 0,
            "session_detail": 0, "materials": 0, "material_detail": 0,
            "mutation": 0, "learning_mutation": 0, "proposal_review": 0,
            "skill_search": 0, "review_mutation": 0}
        self.state.selected_week_start = self._monday(self.clock())
        self.week_day = self.state.selected_week_start

    def _monday(self, instant):
        local = instant.astimezone(ZoneInfo(self.semester.timezone)).date()
        return local - timedelta(days=local.weekday())

    def navigate(self, route: Route):
        if route != self.state.route:
            self._navigation_epoch += 1
            if self.state.route == Route.HOME:
                self._generation["home"] += 1
                self.state.busy[Route.HOME] = Busy.IDLE
            for kind in ("material_detail", "session_detail", "proposal_review", "skill_search"):
                self._generation[kind] += 1
            for detail_route in (Route.MATERIAL_DETAIL, Route.SESSION_DETAIL, Route.PROPOSAL_REVIEW):
                if self.state.busy[detail_route] == Busy.READING:
                    self.state.busy[detail_route] = Busy.IDLE
            self._pending_import_material = None
            self._pending_proposal_review = None
        self.state.route = route
        self.state_changed.emit()
        if route == Route.HOME:
            self.refresh_home()
        if route == Route.WEEK and self.week is None:
            self.refresh_week()
        if route == Route.SESSIONS:
            self.refresh_sessions()
        if route == Route.SESSION_DETAIL and self.state.selected_session_id:
            self.refresh_session_detail()
        if route == Route.MATERIALS and self.materials is None:
            self.refresh_materials()
        if route == Route.MATERIAL_DETAIL and self.state.selected_material_id:
            self.refresh_material_detail()
        if route == Route.PROPOSAL_REVIEW and self.state.selected_proposal_id and self.proposal_review is None:
            self.refresh_proposal_review()

    @staticmethod
    def _materials_default_range(context, instant):
        today = instant.astimezone(ZoneInfo(context.timezone)).date()
        anchor = max(today, context.starts_on) if context.starts_on else today
        anchor = min(anchor, context.ends_on) if context.ends_on else anchor
        start = max(anchor - timedelta(days=30), context.starts_on) if context.starts_on else anchor - timedelta(days=30)
        end = min(anchor + timedelta(days=180), context.ends_on) if context.ends_on else anchor + timedelta(days=180)
        return start, min(max(start, end), start + timedelta(days=365))

    def refresh_materials(self, *, as_of=None):
        instant = as_of or self.clock()
        self._generation["materials"] += 1
        token = (self._generation["materials"], self.state.semester_stable_id,
            self.state.materials_course_stable_id, self.state.materials_from_date,
            self.state.materials_through_date)
        self.state.busy[Route.MATERIALS] = Busy.READING
        self.state_changed.emit()
        def read():
            context = get_materials_context(self.config, token[1], as_of=instant)
            start, end = (token[3], token[4]) if token[3] is not None and token[4] is not None else self._materials_default_range(context, instant)
            inbox = get_material_inbox(self.config, token[1], as_of=instant,
                from_date=start, through_date=end, course_id=token[2])
            return context, inbox, instant
        self.runner.submit("materials", token, read, self._materials_completed)

    def set_materials_filter(self, *, course_id=None, from_date=None, through_date=None):
        start = from_date if from_date is not None else self.state.materials_from_date
        end = through_date if through_date is not None else self.state.materials_through_date
        if start is not None and end is not None and (start > end or (end - start).days >= 366):
            error = PresentationError("INVALID_MATERIAL_INBOX_RANGE", "Check Materials dates",
                "From must be on or before Through, within a 366-day window.")
            self.state.last_presentation_error = error
            self.feedback.emit(error)
            return False
        if (course_id, start, end) == (self.state.materials_course_stable_id,
                self.state.materials_from_date, self.state.materials_through_date) and self.materials is not None:
            return True
        self._navigation_epoch += 1
        self._pending_import_material = None
        self.state.materials_course_stable_id = course_id
        self.state.materials_from_date, self.state.materials_through_date = start, end
        self._generation["material_detail"] += 1
        self.material_detail = None
        if self.state.route == Route.MATERIAL_DETAIL:
            self.material_detail_changed.emit(None)
        if self.state.selected_material_id and course_id is not None and self.materials:
            chosen = next((m for m in self.materials.materials if m.material_id == self.state.selected_material_id), None)
            if chosen is None or chosen.course_stable_id != course_id:
                self._clear_material_selection()
        self.materials = None
        self.refresh_materials()
        return True

    def _set_selection(self, field, value):
        # Even reselecting the same row opens a new context. IDs alone can be
        # reused after X -> Y -> X; this generation can never be restored.
        self._selection_generation += 1
        setattr(self.state, field, value)
        self._pending_proposal_review = None
        for kind in ("material_detail", "session_detail", "proposal_review", "skill_search"):
            self._generation[kind] += 1
        for route in (Route.MATERIAL_DETAIL, Route.SESSION_DETAIL, Route.PROPOSAL_REVIEW):
            if self.state.busy[route] == Busy.READING:
                self.state.busy[route] = Busy.IDLE
        # The mutation has already completed when an authoritative readback is
        # pending. Abandoning that read must not leave review actions locked.
        if self._review_readback_identity is not None:
            self._review_readback_identity = None
            self.review_mutation_busy = False

    def _clear_material_selection(self):
        self._set_selection("selected_material_id", None)
        self.material_detail = None
        self._generation["material_detail"] += 1
        if self.state.route == Route.MATERIAL_DETAIL:
            self._navigation_epoch += 1
            self.state.route = Route.MATERIALS
            self.state_changed.emit()

    def select_material(self, material_id):
        if not self.materials or not any(m.material_id == material_id for m in self.materials.materials):
            return
        self._set_selection("selected_material_id", material_id)
        self.material_detail = None
        self.material_detail_changed.emit(None)
        self.navigate(Route.MATERIAL_DETAIL)

    def open_weekly_drop(self, drop):
        self.set_materials_filter(course_id=drop.course_stable_id)
        self.navigate(Route.MATERIALS)
        if drop.material_id:
            self._pending_import_material = (drop.material_id, self._navigation_epoch)
            if self.materials and any(m.material_id == drop.material_id for m in self.materials.materials):
                self._pending_import_material = None
                self.select_material(drop.material_id)

    def back_to_materials(self):
        self._clear_material_selection()
        self.navigate(Route.MATERIALS)

    def refresh_material_detail(self, *, as_of=None):
        if not self.state.selected_material_id or not self.state.materials_from_date:
            return
        self._generation["material_detail"] += 1
        token = (self._generation["material_detail"], self.state.semester_stable_id,
            self.state.materials_course_stable_id, self.state.materials_from_date,
            self.state.materials_through_date, self.state.selected_material_id, self.workflow_identity())
        instant = as_of or self.materials_as_of or self.clock()
        self.state.busy[Route.MATERIAL_DETAIL] = Busy.READING
        self.state_changed.emit()
        self.runner.submit("material_detail", token,
            lambda: get_material_detail(self.config, token[1], token[5], as_of=instant,
                from_date=token[3], through_date=token[4]), self._material_detail_completed)

    def _materials_completed(self, kind, token, result):
        current = (self._generation["materials"], self.state.semester_stable_id,
            self.state.materials_course_stable_id, self.state.materials_from_date,
            self.state.materials_through_date)
        if token != current:
            return
        self.state.busy[Route.MATERIALS] = Busy.IDLE
        if isinstance(result, PresentationError):
            self._pending_import_material = None
            self.materials = None
            self.materials_changed.emit(None)
            if self.state.route == Route.MATERIAL_DETAIL:
                self._clear_material_selection()
                self.material_detail_changed.emit(None)
            self.feedback.emit(result)
        else:
            self.materials_context, self.materials, self.materials_as_of = result
            self.state.materials_from_date = self.materials.from_date
            self.state.materials_through_date = self.materials.through_date
            if self.state.selected_material_id and not any(
                    m.material_id == self.state.selected_material_id for m in self.materials.materials):
                self._clear_material_selection()
            self.materials_changed.emit(result[:2])
            pending = self._pending_import_material
            if (pending and pending[1] == self._navigation_epoch and self.state.route == Route.MATERIALS
                    and any(m.material_id == pending[0] for m in self.materials.materials)):
                self._pending_import_material = None
                self.select_material(pending[0])
                self.state_changed.emit()
                return
            if self.state.route == Route.MATERIAL_DETAIL and self.state.selected_material_id:
                self.refresh_material_detail(as_of=self.materials_as_of)
        self.state_changed.emit()

    def _material_detail_completed(self, kind, token, result):
        current = (self._generation["material_detail"], self.state.semester_stable_id,
            self.state.materials_course_stable_id, self.state.materials_from_date,
            self.state.materials_through_date, self.state.selected_material_id, self.workflow_identity())
        if token != current:
            return
        self.state.busy[Route.MATERIAL_DETAIL] = Busy.IDLE
        if isinstance(result, PresentationError):
            self.material_detail = None
            self.material_detail_changed.emit(None)
            self.feedback.emit(result)
        else:
            self.material_detail = result
            self.material_detail_changed.emit((self.materials_context, result))
            pending = self._pending_proposal_review
            if pending and (pending[1], pending[2], self.state.route) == (
                    self._navigation_epoch, token[5], Route.MATERIAL_DETAIL) and any(
                    proposal.proposal_id == pending[0] for proposal in result.skill_proposals):
                self._pending_proposal_review = None
                self.select_proposal_review(ReviewFamily.SKILL, pending[0], Route.MATERIAL_DETAIL)
        self.state_changed.emit()

    def select_proposal_review(self, family, proposal_id, return_route=Route.MATERIALS):
        family = ReviewFamily(family)
        if return_route not in {Route.MATERIALS, Route.MATERIAL_DETAIL}:
            return False
        # Entry points are scoped DTO rows. Never open project-wide summaries.
        if return_route == Route.MATERIALS:
            rows = ((self.materials.pending_classification_proposals + self.materials.decided_classification_proposals)
                if family == ReviewFamily.CLASSIFICATION else
                (self.materials.pending_skill_proposals + self.materials.decided_skill_proposals)) if self.materials else []
        else:
            rows = ((self.material_detail.classification_proposals if family == ReviewFamily.CLASSIFICATION
                else self.material_detail.skill_proposals) if self.material_detail else [])
        if not any(p.proposal_id == proposal_id for p in rows):
            return False
        self._generation["proposal_review"] += 1
        self._generation["skill_search"] += 1
        if self.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING:
            self.state.busy[Route.PROPOSAL_REVIEW] = Busy.IDLE
        self.state.selected_review_family = family
        self._set_selection("selected_proposal_id", proposal_id)
        self.state.review_return_route = return_route
        self.proposal_review = None
        self.skill_choices = None
        self.selected_review_skill_id = None
        self.proposal_review_changed.emit(None)
        self.skill_choices_changed.emit(None)
        self.review_opened.emit()
        self.navigate(Route.PROPOSAL_REVIEW)
        return True

    def back_from_proposal_review(self):
        origin = self.state.review_return_route
        self._generation["proposal_review"] += 1
        self._generation["skill_search"] += 1
        if self.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING:
            self.state.busy[Route.PROPOSAL_REVIEW] = Busy.IDLE
        self.state.selected_review_family = None
        self._set_selection("selected_proposal_id", None)
        self.proposal_review = None
        self.skill_choices = None
        self.selected_review_skill_id = None
        if origin == Route.MATERIAL_DETAIL and self.state.selected_material_id and self.material_detail is not None:
            self.navigate(Route.MATERIAL_DETAIL)
        else:
            self.navigate(Route.MATERIALS)

    def refresh_proposal_review(self, *, authoritative=False):
        family = self.state.selected_review_family
        proposal_id = self.state.selected_proposal_id
        if family is None or proposal_id is None:
            return
        self._generation["proposal_review"] += 1
        token = (self._generation["proposal_review"], self.state.semester_stable_id,
            family, proposal_id, self.workflow_identity())
        if authoritative:
            self._review_readback_identity = token
        self.proposal_review = None
        self.state.busy[Route.PROPOSAL_REVIEW] = Busy.READING
        self.proposal_review_changed.emit(None)
        self.state_changed.emit()
        fn = get_classification_review if family == ReviewFamily.CLASSIFICATION else get_skill_review
        self.runner.submit("proposal_review", token,
            lambda: fn(self.config, token[1], token[3]), self._proposal_review_completed)
        return token

    def _proposal_review_completed(self, kind, token, result):
        if token == self._review_readback_identity:
            self.review_mutation_busy = False
            self._review_readback_identity = None
        current = (self._generation["proposal_review"], self.state.semester_stable_id,
            self.state.selected_review_family, self.state.selected_proposal_id, self.workflow_identity())
        if token != current:
            self.state_changed.emit()
            return
        self.state.busy[Route.PROPOSAL_REVIEW] = Busy.IDLE
        if isinstance(result, PresentationError):
            self.proposal_review = None
            self.proposal_review_changed.emit(None)
            self.feedback.emit(result)
        else:
            self.proposal_review = result
            self.proposal_review_changed.emit(result)
        self.state_changed.emit()

    def search_review_skills(self, query):
        if (self.state.selected_review_family != ReviewFamily.SKILL or
                not self.state.selected_proposal_id or self.proposal_review is None or
                self.proposal_review.proposal.proposal_kind != "NEW_SKILL"):
            return False
        if not isinstance(query, str) or len(query) > 120 or query != query.strip():
            self.feedback.emit(PresentationError("INVALID_SKILL_CHOICE_QUERY", "Check search",
                "Use at most 120 characters, without leading or trailing spaces."))
            return False
        self._generation["skill_search"] += 1
        token = (self._generation["skill_search"], self.state.semester_stable_id,
            self.state.selected_proposal_id, query, self.workflow_identity())
        self.skill_choices = None
        self.selected_review_skill_id = None
        self.skill_choices_changed.emit(None)
        if not query:
            return True
        self.runner.submit("skill_search", token,
            lambda: search_active_skills(self.config, query=query, limit=20), self._skill_search_completed)
        return True

    def invalidate_review_skill_search(self):
        self._generation["skill_search"] += 1
        self.skill_choices = None
        self.selected_review_skill_id = None
        self.skill_choices_changed.emit(None)

    def _skill_search_completed(self, kind, token, result):
        current = (self._generation["skill_search"], self.state.semester_stable_id,
            self.state.selected_proposal_id, token[3], self.workflow_identity())
        if token != current or self.state.selected_review_family != ReviewFamily.SKILL:
            return
        if isinstance(result, PresentationError):
            self.feedback.emit(result)
        else:
            self.skill_choices = result
            self.skill_choices_changed.emit(result)

    def select_review_skill(self, skill_id):
        if self.review_mutation_busy:
            return False
        if self.skill_choices is None or not any(x.skill_id == skill_id for x in self.skill_choices.choices):
            return False
        self.selected_review_skill_id = skill_id
        self.skill_choices_changed.emit(self.skill_choices)
        return True

    def decide_proposal(self, action, *, reason=None):
        review = self.proposal_review
        family = self.state.selected_review_family
        if (self.review_mutation_busy or review is None or family is None or
                review.proposal.proposal_id != self.state.selected_proposal_id or
                review.proposal.status != "PENDING" or action not in review.supported_actions):
            return False
        base = dict(semester_id=self.state.semester_stable_id,
            proposal_id=review.proposal.proposal_id,
            expected_review_context_token=review.review_context_token)
        if family == ReviewFamily.CLASSIFICATION:
            model, fn = ((ClassificationAcceptRequestV1, review_workflow.accept_classification)
                if action == "ACCEPT" else (ClassificationRejectRequestV1, review_workflow.reject_classification))
            base["reason"] = reason
        else:
            if action == "ACCEPT_NEW":
                model, fn = SkillAcceptNewRequestV1, review_workflow.accept_new_skill
            elif action == "RESOLVE_EXISTING":
                if self.selected_review_skill_id is None:
                    return False
                model, fn = SkillResolveExistingRequestV1, review_workflow.resolve_existing_skill
                base["existing_skill_id"] = self.selected_review_skill_id
            else:
                model, fn = SkillRejectRequestV1, review_workflow.reject_skill
                base["reason"] = reason
        self.review_mutation_busy = True
        self._generation["review_mutation"] += 1
        token = (self._generation["review_mutation"], self.state.semester_stable_id,
            family, review.proposal.proposal_id, base.get("existing_skill_id"), self.workflow_identity())
        self.state.busy[Route.PROPOSAL_REVIEW] = Busy.MUTATING
        self.state_changed.emit()
        self.runner.submit("review_mutation", token, lambda: fn(self.config, model(**base)),
            lambda kind, tok, result: self._review_mutation_completed(action, tok, result))
        return True

    def _review_mutation_completed(self, action, token, result):
        if token[0] != self._generation["review_mutation"]:
            return
        same = token[5] == self.workflow_identity()
        if not same:
            self.review_mutation_busy = False
            if self.state.busy[Route.PROPOSAL_REVIEW] == Busy.MUTATING:
                self.state.busy[Route.PROPOSAL_REVIEW] = Busy.IDLE
            self.feedback.emit(result if isinstance(result, PresentationError) else
                "Review decision saved for the previous selection. Refresh to see its history.")
            self.state_changed.emit()
            return
        self._review_readback_identity = None
        if isinstance(result, PresentationError):
            self.feedback.emit(result)
        else:
            messages = {"ACCEPTED": "Proposal accepted", "ALREADY_ACCEPTED": "Proposal already accepted",
                "REJECTED": "Proposal rejected", "ALREADY_REJECTED": "Proposal already rejected"}
            message = messages[result.outcome]
            if action == "ACCEPT_NEW" and result.outcome == "ACCEPTED":
                message = "Skill bestätigt. Bitte im Materialdetail dem Fach zuordnen."
            elif action == "RESOLVE_EXISTING" and result.outcome == "ACCEPTED":
                message = "Proposal resolved to existing Skill"
            self.feedback.emit(message)
        refresh_codes = {"REVIEW_CONTEXT_CHANGED", "REVIEW_TARGET_CONFLICT", "REVIEW_REASON_CONFLICT",
            "REVIEW_PROPOSAL_NOT_PENDING", "REVIEW_SKILL_NOT_ACTIVE"}
        refresh = not isinstance(result, PresentationError) or result.code in refresh_codes
        if refresh:
            if self.materials is not None:
                self.refresh_materials()
            if self.material_detail is not None and self.state.selected_material_id:
                self.refresh_material_detail()
            if (not isinstance(result, PresentationError) and token[2] == ReviewFamily.CLASSIFICATION
                    and action == "ACCEPT"):
                instant = self.clock()
                if self.home is not None:
                    self.refresh_home(as_of=instant)
                if self.week is not None:
                    self.refresh_week(as_of=instant)
            if same:
                self._generation["skill_search"] += 1
                self.skill_choices = None
                self.selected_review_skill_id = None
                self.skill_choices_changed.emit(None)
                self.refresh_proposal_review(authoritative=True)
                return
        self.review_mutation_busy = False
        self._review_readback_identity = None
        if not (self.state.route == Route.PROPOSAL_REVIEW and
                self.state.busy[Route.PROPOSAL_REVIEW] == Busy.READING):
            self.state.busy[Route.PROPOSAL_REVIEW] = Busy.IDLE
        self.state_changed.emit()

    def select_session(self, session_id: str):
        self._set_selection("selected_session_id", session_id)
        self.session_detail = None
        self.navigate(Route.SESSION_DETAIL)

    def back_to_sessions(self):
        self._set_selection("selected_session_id", None)
        self._generation["session_detail"] += 1
        self.navigate(Route.SESSIONS)

    def select_week(self, monday: date):
        if monday.weekday() != 0:
            raise ValueError("Week must start on Monday")
        if self.state.busy[Route.WEEK] == Busy.MUTATING:
            return
        self.state.selected_week_start = monday
        self.week_day = monday
        self.week = None
        self.refresh_week()

    def open_week_day(self, day: date):
        monday = day - timedelta(days=day.weekday())
        if self.state.busy[Route.WEEK] == Busy.MUTATING:
            return False
        if monday != self.state.selected_week_start:
            self.state.selected_week_start = monday
            self.week = None
        self.week_day = day
        self.navigate(Route.WEEK)
        if self.week is not None:
            self.week_changed.emit(self.week)
        return True

    def close_home(self):
        self._closed = True
        self._generation["home"] += 1

    def refresh_home(self, *, as_of=None):
        if self._closed:
            return
        if self.state.route == Route.HOME:
            self.home_loading.emit()
        self._read("home", as_of or self.clock())

    def refresh_week(self, *, as_of=None):
        self._read("week", as_of or self.clock())

    def refresh_sessions(self, *, as_of=None):
        self._read("sessions", as_of or self.clock())

    def refresh_session_detail(self, *, as_of=None):
        if self.state.selected_session_id:
            self._read("session_detail", as_of or self.clock())

    def refresh_home_and_week(self):
        instant = self.clock()
        self.refresh_week(as_of=instant)
        self.refresh_home(as_of=instant)

    def _read(self, kind, as_of):
        self._generation[kind] += 1
        token = (self._generation[kind], self.state.semester_stable_id,
                 self.state.selected_week_start if kind == "week" else
                 self.state.selected_session_id if kind == "session_detail" else None,
                 self.workflow_identity() if kind == "session_detail" else None)
        route = Route(kind)
        self.state.busy[route] = Busy.READING
        self.state_changed.emit()
        if kind == "home":
            def fn():
                home = get_home_dashboard(self.config, token[1], as_of=as_of, include_academic=False)
                return home, project_dashboard(self.config, token[1], as_of)
        elif kind == "week":
            fn = lambda: get_week_view(self.config, token[1], token[2], as_of=as_of)
        elif kind == "sessions":
            fn = lambda: get_session_inbox(self.config, token[1], as_of=as_of)
        else:
            fn = lambda: get_session_detail(self.config, token[1], token[2], as_of=as_of)
        self.runner.submit(kind, token, fn, self._completed)

    def _mutate(self, operation, request, fn):
        if self.state.busy[Route.WEEK] != Busy.IDLE:
            return False
        self._generation["mutation"] += 1
        token = (self._generation["mutation"], self.state.semester_stable_id,
                 self.state.selected_week_start)
        self.state.busy[Route.WEEK] = Busy.MUTATING
        self.state_changed.emit()
        self.runner.submit("mutation", token, lambda: fn(self.config, request),
            lambda kind, tok, result: self._mutation_completed(operation, tok, result))
        return True

    def _request(self, model, **fields):
        try:
            return model(**fields)
        except ValidationError:
            error = PresentationError("WORKFLOW_INPUT_INVALID", "Check planning input",
                "The planning input is invalid. Review the fields and try again.")
            self.state.last_presentation_error = error
            self.feedback.emit(error)
            return None

    def create_plan(self):
        if (self.week is None or self.week.current_calendar.planning_profile_id is None or
                not self.week.current_calendar.availability_occurrences):
            self.feedback.emit(PresentationError("NO_LEARNING_TIMES", "Lernzeiten fehlen",
                "Zuerst ein Planning Profile und mindestens ein Lernzeitfenster einrichten."))
            return False
        request = self._request(CreatePlanRequestV1, semester_id=self.state.semester_stable_id,
            week_start=self.state.selected_week_start, as_of=self.clock())
        return self._mutate("create", request, planning_workflow.create_plan) if request else False

    def create_planning_profile(self, duration_minutes, daily_minutes):
        values = (self.state.semester_stable_id, duration_minutes, daily_minutes)
        return self._mutate("profile", values, lambda config, data:
            planning_calendar.create_planning_profile(config, data[0],
                default_action_duration_minutes=data[1], max_daily_study_minutes=data[2]))

    def add_learning_window(self, weekday, start, end):
        values = (self.state.semester_stable_id, weekday, start, end)
        return self._mutate("availability", values, lambda config, data:
            planning_calendar.add_availability_window(config, data[0], weekday=data[1],
                start_local_time=data[2], end_local_time=data[3]))

    def deactivate_learning_window(self, window_id):
        return self._mutate("availability_remove", window_id, lambda config, identifier:
            planning_calendar.deactivate_availability_window(config, identifier))

    def regenerate_plan(self, plan_id, *, course_filter_mode, course_id=None,
                        duration_overrides_mode, duration_overrides=None, limit=None):
        request = self._request(RegeneratePlanRequestV1, plan_id=plan_id, as_of=self.clock(),
            course_filter_mode=course_filter_mode, course_id=course_id,
            duration_overrides_mode=duration_overrides_mode,
            duration_overrides=duration_overrides, limit=limit)
        return self._mutate("regenerate", request, planning_workflow.regenerate_plan) if request else False

    def move_block(self, plan_id, revision_id, block_id, new_start):
        request = self._request(MoveBlockRequestV1, plan_id=plan_id, expected_revision_id=revision_id,
            block_id=block_id, new_start=new_start)
        return self._mutate("move", request, planning_workflow.move_block) if request else False

    def set_block_duration(self, plan_id, revision_id, block_id, minutes):
        request = self._request(SetBlockDurationRequestV1, plan_id=plan_id, expected_revision_id=revision_id,
            block_id=block_id, minutes=minutes)
        return self._mutate("duration", request, planning_workflow.set_block_duration) if request else False

    def remove_block(self, plan_id, revision_id, block_id):
        request = self._request(RemoveBlockRequestV1, plan_id=plan_id, expected_revision_id=revision_id,
            block_id=block_id)
        return self._mutate("remove", request, planning_workflow.remove_block) if request else False

    def place_unscheduled(self, plan_id, revision_id, action_id, start, duration_minutes=None):
        request = self._request(PlaceUnscheduledRequestV1, plan_id=plan_id,
            expected_revision_id=revision_id, action_id=action_id, start=start,
            duration_minutes=duration_minutes)
        return self._mutate("place", request, planning_workflow.place_unscheduled) if request else False

    def accept_plan(self, plan_id, revision_id):
        request = self._request(AcceptPlanRequestV1, plan_id=plan_id, revision_id=revision_id,
            accepted_at=self.clock())
        return self._mutate("accept", request, planning_workflow.accept_plan) if request else False

    def materialize_block(self, plan_id, accepted_revision_id, block_id, goal=None):
        request = self._request(MaterializeBlockRequestV1, plan_id=plan_id, block_id=block_id,
            expected_accepted_revision_id=accepted_revision_id, goal=goal,
            materialized_at=self.clock())
        return self._mutate("materialize", request, planning_workflow.materialize_block) if request else False

    def _learning_request(self, model, **fields):
        try:
            return model(**fields)
        except ValidationError:
            error = PresentationError("WORKFLOW_INPUT_INVALID", "Check learning input",
                "The learning action input is invalid. Refresh and try again.")
            self.state.last_presentation_error = error
            self.feedback.emit(error)
            return None

    def import_study_material(self, path, course_stable_id):
        if self.bridge_busy:
            return False
        self._import_origin = (self._navigation_epoch, self.state.route,
            self.state.materials_course_stable_id, self.state.materials_from_date,
            self.state.materials_through_date, course_stable_id)
        semester_id = self.state.semester_stable_id
        def run():
            if course_stable_id not in dict(gui_learning_bridge.semester_courses(self.config, semester_id)):
                from uniflow.application.workflow_errors import WorkflowCommandError
                raise WorkflowCommandError("INVALID_SESSION_COURSE")
            return gui_learning_bridge.import_and_parse(self.config, path, course_stable_id)
        return self._bridge("import", run)

    def create_study_session(self, course_stable_id, goal, skill_ids):
        if self.bridge_busy:
            return False
        semester_id = self.state.semester_stable_id
        targets = list(skill_ids)
        return self._bridge("session", lambda: gui_learning_bridge.create_course_study_session(
            self.config, semester_id, course_stable_id, goal, targets))

    def workflow_identity(self):
        """Identity captured before a worker or nested Qt dialog starts."""
        return (self._navigation_epoch, self.state.semester_stable_id, self.state.route,
            self.state.selected_material_id, self.state.selected_proposal_id,
            self.state.selected_session_id, self._selection_generation)

    def read_study_data(self, fn, callback, *, finished=None):
        identity = self.workflow_identity()
        delivered = False
        def completed(kind, token, result):
            nonlocal delivered
            if delivered:
                return
            delivered = True
            try:
                if token != self.workflow_identity():
                    return
                if isinstance(result, PresentationError):
                    self.feedback.emit(result)
                else:
                    callback(result)
            finally:
                if finished:
                    finished()
        self.runner.submit("study_read", identity, fn, completed)

    def propose_study_skill(self, material_id, name, kind, quote):
        return self._bridge("proposal", lambda: gui_learning_bridge.propose_document_skill(
            self.config, material_id, name, kind, quote))

    def propose_selected_study_skill(self, material_id, parse_run_id, unit_id,
            start, end, name, kind):
        return self._bridge("proposal", lambda: gui_learning_bridge.propose_selected_document_skill(
            self.config, material_id, parse_run_id, unit_id, start, end, name, kind))

    def associate_study_skill(self, material_id, skill_id):
        return self._bridge("association", lambda: gui_learning_bridge.associate_skill_with_course(
            self.config, material_id, skill_id))

    def _bridge(self, operation, fn):
        if self.bridge_busy:
            return False
        self.bridge_busy = True
        self._bridge_generation += 1
        token = (self._bridge_generation, operation, self.workflow_identity())
        self.state_changed.emit()
        self.runner.submit("study_bridge", token, fn, self._bridge_completed)
        return True

    def _bridge_completed(self, operation, token, result):
        if token[0] != self._bridge_generation:
            return
        operation = token[1]
        same = token[2] == self.workflow_identity()
        self.bridge_busy = False
        if not same:
            if (operation == "import" and token[2][1] == self.state.semester_stable_id
                    and self.state.selected_material_id is None):
                # Refresh the inbox only; never reopen the imported Material.
                self._pending_import_material = None
                self.refresh_materials()
            self.feedback.emit(result if isinstance(result, PresentationError) else
                "Learning action completed for the previous selection. Refresh it to see the result.")
            self.state_changed.emit()
            return
        if isinstance(result, PresentationError):
            self.feedback.emit(result)
        elif operation == "import":
            material_id, status, error = result
            message = ("Material imported and parsed" if status == "SUCCESS" else
                "Material imported; scanned PDF needs OCR before Skill review" if status == "OCR_REQUIRED" else
                "Material imported; text parsing needs review" if status == "FAILED" else
                f"Material imported · parse {status}")
            self.feedback.emit(message + (f" · {error}" if error else ""))
            origin = self._import_origin
            self._import_origin = None
            if (origin and origin[:5] == (self._navigation_epoch, self.state.route,
                    self.state.materials_course_stable_id, self.state.materials_from_date,
                    self.state.materials_through_date) and self.state.route == Route.MATERIALS):
                self._pending_import_material = (material_id, self._navigation_epoch)
                if self.state.materials_course_stable_id not in (None, origin[5]):
                    self.state.materials_course_stable_id = None
            self.refresh_materials()
            self.refresh_home()
        elif operation == "session":
            self.feedback.emit("Learning Session created")
            self.refresh_sessions()
            if self.home is not None:
                self.refresh_home()
            if self.week is not None:
                self.refresh_week()
            self.select_session(result.session_id)
        elif operation == "proposal":
            self.feedback.emit("Skill-Vorschlag gespeichert. Bitte die Quellenstelle im Review prüfen und bestätigen.")
            if self.state.route == Route.MATERIAL_DETAIL and self.state.selected_material_id:
                self._pending_proposal_review = (result, self._navigation_epoch,
                    self.state.selected_material_id)
            self.refresh_materials()
            self.refresh_material_detail()
        elif operation == "association":
            self.feedback.emit("Skill wurde dem Fach zugeordnet. Jetzt kann eine Lernsession vorbereitet werden.")
            self.refresh_materials()
            self.refresh_material_detail()
            self.refresh_home()
            self.refresh_week()
        self.state_changed.emit()

    def _learn(self, operation, request, fn):
        if request is None or self.learning_mutation_busy or not self.state.selected_session_id:
            return False
        self.learning_mutation_busy = True
        self._generation["learning_mutation"] += 1
        token = (self._generation["learning_mutation"], self.state.semester_stable_id,
            self.state.selected_session_id, self.workflow_identity())
        self.state.busy[Route.SESSION_DETAIL] = Busy.MUTATING
        self.state_changed.emit()
        self.runner.submit("learning_mutation", token,
            lambda: fn(self.config, request() if callable(request) else request),
            lambda kind, tok, result: self._learning_completed(operation, tok, result))
        return True

    def generate_learning_package(self):
        session_id = self.state.selected_session_id
        return self._learn("generate", lambda: GenerateHandoffRequestV1(
            session_id=session_id), learning_workflow.generate_handoff)

    def export_learning_package(self, parent):
        handoff = self.session_detail.session.current_handoff if self.session_detail else None
        if handoff is None:
            return False
        return self._learn("export", self._learning_request(ExportExchangeRequestV1,
            handoff_id=handoff.handoff_id, destination_directory=parent), learning_workflow.export_exchange)

    def export_learning_chat(self, parent):
        detail = self.session_detail
        if (detail is None or detail.session.session_id != self.state.selected_session_id
                or detail.session.current_handoff is None):
            return False
        handoff_id = detail.session.current_handoff.handoff_id
        return self._learn("chat_export", lambda: ExportExchangeRequestV1(
            handoff_id=handoff_id, destination_directory=parent), learning_workflow.export_learning_chat)

    def start_learning_session(self):
        return self._learn("start", self._learning_request(StartSessionRequestV1,
            session_id=self.state.selected_session_id), learning_workflow.start_session)

    def import_learning_result(self, path):
        return self._learn("import", self._learning_request(ImportResultFileRequestV1,
            path=path), learning_workflow.import_result_file)

    def apply_learning_mastery(self):
        result = self.session_detail.session.result if self.session_detail else None
        if result is None:
            return False
        return self._learn("apply", self._learning_request(ApplyMasteryRequestV1,
            result_id=result.result_id), learning_workflow.apply_mastery)

    def _week_mentions_session(self, session_id):
        if self.week is None:
            return False
        accepted = self.week.accepted_plan
        return bool(accepted and accepted.blocks and any(
            block.learning_session_id == session_id for block in accepted.blocks))

    def _learning_completed(self, operation, token, result):
        if token[0] != self._generation["learning_mutation"]:
            return
        self.learning_mutation_busy = False
        if self.state.busy[Route.SESSION_DETAIL] == Busy.MUTATING:
            self.state.busy[Route.SESSION_DETAIL] = Busy.IDLE
        same_selection = token[3] == self.workflow_identity()
        if not same_selection:
            self.feedback.emit(result if isinstance(result, PresentationError) else
                "Learning action completed for the previous Session. Refresh it to see the result.")
            self.state_changed.emit()
            return
        if operation == "chat_export" and not isinstance(result, PresentationError):
            self.feedback.emit(f"Learning chat {result.status} · {result.path} · Session {result.session_id} · "
                f"Handoff {result.handoff_id} · SHA-256 {result.handoff_artifact_sha256} · "
                f"Return filename {result.result_filename}")
            self.state_changed.emit()
            return
        if isinstance(result, PresentationError):
            self.state.last_presentation_error = result
            self.feedback.emit(result)
            if result.code in {"INVALID_SESSION_STATE", "HANDOFF_FROZEN", "RESULT_PROCESSING_STATE_INVALID",
                    "EXCHANGE_HANDOFF_NOT_CURRENT", "HANDOFF_CONTEXT_CHANGED", "RESULT_CONTEXT_CHANGED"}:
                instant = self.clock()
                self.refresh_sessions(as_of=instant)
                if same_selection:
                    self.refresh_session_detail(as_of=instant)
        else:
            messages = {"GENERATED": "Learning package generated", "ALREADY_GENERATED": "Learning package already current",
                "EXPORTED": "Learning package ready", "ALREADY_EXPORTED": "Package already exported",
                "STARTED": "Session started", "ALREADY_STARTED": "Session already started",
                "IMPORTED": "Result imported", "ALREADY_IMPORTED": "Result already imported",
                "APPLIED": "Result processed", "ALREADY_APPLIED": "Result already processed"}
            message = messages[result.outcome]
            if operation == "export":
                message += " · " + result.directory_name
            elif operation == "import":
                message += f" · Evidence items: {result.evidence_count}"
            elif operation == "apply" and result.skipped_skill_ids:
                names = {x.skill_id: x.name for x in self.session_detail.target_labels} if same_selection and self.session_detail else {}
                message += " · No new mastery value for: " + ", ".join(names.get(x, x) for x in result.skipped_skill_ids)
            self.feedback.emit(message)
            if operation != "export":
                instant = self.clock()
                self.refresh_sessions(as_of=instant)
                if same_selection:
                    self.refresh_session_detail(as_of=instant)
                if self.home is not None:
                    self.refresh_home(as_of=instant)
                if self.week is not None and (operation in {"start", "import", "apply"} or
                        self._week_mentions_session(token[2])):
                    self.refresh_week(as_of=instant)
        self.state_changed.emit()

    def _mutation_completed(self, operation, token, result):
        if token[0] != self._generation["mutation"] or token[1] != self.state.semester_stable_id:
            return
        if token[2] != self.state.selected_week_start:
            return
        self.state.busy[Route.WEEK] = Busy.IDLE
        if isinstance(result, PresentationError):
            self.state.last_presentation_error = result
            self.feedback.emit(result)
            if result.code in {"REVISION_CHANGED", "ACCEPTED_REVISION_CHANGED", "REVISION_NOT_CURRENT", "PLAN_ALREADY_EXISTS"}:
                self.refresh_home_and_week()
        else:
            if operation == "materialize":
                message = ("Existing Session reused" if result.outcome == "ALREADY_MATERIALIZED"
                    else "Session created" if result.execution_kind == "CREATED_SESSION" else "Session resumed")
                message += f" · {result.session_state.replace('_', ' ').title()}"
            elif operation == "accept":
                message = "Plan already accepted" if result.outcome == "ALREADY_ACCEPTED" else "Plan accepted"
            else:
                message = {"profile": "Planning Profile angelegt. Jetzt Lernzeitfenster eintragen.",
                    "availability": "Lernzeitfenster gespeichert.",
                    "availability_remove": "Lernzeitfenster deaktiviert.",
                    "create": "Plan created", "regenerate": "Plan regenerated",
                    "move": "Draft updated", "duration": "Draft updated",
                    "remove": "Block removed from new draft", "place": "Action placed in new draft"}[operation]
            self.feedback.emit(message)
            self.refresh_home_and_week()
        self.state_changed.emit()

    def _completed(self, kind, token, result):
        if kind == "home" and self._closed:
            return
        if token[0] != self._generation[kind] or token[1] != self.state.semester_stable_id:
            return
        if kind == "week" and token[2] != self.state.selected_week_start:
            return
        if kind == "session_detail" and token[3] != self.workflow_identity():
            return
        route = Route(kind)
        if self.state.busy[route] == Busy.MUTATING:
            return
        self.state.busy[route] = Busy.IDLE
        if isinstance(result, PresentationError):
            if result.code == "APPLICATION_FAILURE":
                action = {"home": "Dashboard", "week": "Week", "sessions": "Sessions",
                    "session_detail": "Session detail"}[kind]
                result = PresentationError(result.code, f"{action} could not load",
                    f"{action} could not load. Review the local log for the underlying error and retry.", True)
            self.state.last_presentation_error = result
            if kind != "home" or self.state.route == Route.HOME:
                self.feedback.emit(result)
            if kind == "home" and self.state.route == Route.HOME:
                self.dashboard_changed.emit(result)
        elif kind == "home":
            self.home, self.dashboard_projection = result
            if self.state.route == Route.HOME:
                self.home_changed.emit(self.home)
                self.dashboard_changed.emit(self.dashboard_projection)
        elif kind == "week":
            self.week = result
            self.week_changed.emit(result)
        elif kind == "sessions":
            self.sessions = result
            self.sessions_changed.emit(result)
        else:
            self.session_detail = result
            self.session_detail_changed.emit(result)
        self.state_changed.emit()
