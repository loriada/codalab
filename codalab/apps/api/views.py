"""
Defines Django views for 'apps.api' app.
"""
import json
import logging
from . import serializers
from rest_framework import (permissions, status, viewsets, views)
from rest_framework.decorators import action, link, permission_classes
from rest_framework.exceptions import PermissionDenied, ParseError
from rest_framework.response import Response
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.files.base import ContentFile
from django.http import Http404
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

from apps.jobs.models import Job
from apps.web import models as webmodels
from apps.web.tasks import (create_competition, evaluate_submission)

logger = logging.getLogger(__name__)

@permission_classes((permissions.IsAuthenticated,))
class CompetitionCreationApi(views.APIView):
    """
    Provides a web API to start the process of creating a competition.
    """
    def post(self, request):
        """
        This POST method expects a file identified by the key 'file' in the set of files uploaded
        by the client in multipart MIME format ('multipart/form-data'). The uploaded file is used
        to create a competition definition bundle on behalf of the logged in user. When the bundle
        is created a job is launched to start the process of creating the competition from the
        specified definition. The job ID is returned to the client in a JSON object:
            { 'token': <value> }
        Use the token with CompetitionCreationStatusApi to track the progress of the job.
        """
        uploaded_file = None
        if 'file' in request.FILES:
            uploaded_file = request.FILES['file']
        if uploaded_file is None:
            return Response("Invalid or missing file.", status=status.HTTP_400_BAD_REQUEST)
        owner = self.request.user
        logger.debug("CompetitionCreation: owner=%s; filename=%s.", owner.id, uploaded_file.name)
        cdb = webmodels.CompetitionDefBundle.objects.create(owner=owner, config_bundle=uploaded_file)
        logger.debug("CompetitionCreation def: owner=%s; def=%s; blob=%s.", owner.id, cdb.pk, cdb.config_bundle.name)
        job = create_competition(cdb.pk)
        logger.debug("CompetitionCreation job: owner=%s; def=%s; job=%s.", owner.id, cdb.pk, job.pk)
        return Response({'token' : job.pk }, status=status.HTTP_201_CREATED)

@permission_classes((permissions.IsAuthenticated,))
class CompetitionCreationStatusApi(views.APIView):
    """
    Provides a web API to track progress of a 'create' operation started with CompetitionCreationApi.
    """
    def get(self, request, token):
        """
        Returns the operation status:
           { 'status': <value> }
        where <value> is status of the job as defined by the 'code_name' in apps.jobs.models.Job.STATUS_BY_CODE.
        """
        user_id = self.request.user.id
        logger.debug("CompetitionCreationStatus: requestor=%s; token=%s.", user_id, token)
        try:
            job = Job.objects.get(pk=token)
        except Job.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        logger.debug("CompetitionCreationStatus: requestor=%s; job=%s; job.status:%s.", user_id, job.pk, job.status)
        data = {'status' : job.get_status_code_name() }
        info = job.get_task_info()
        if 'competition_id' in info:
            data['id'] = info['competition_id']
        return Response(data)

class CompetitionAPIViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionSerial
    queryset = webmodels.Competition.objects.all()

    @method_decorator(login_required)
    def destroy(self, request, pk, *args, **kwargs):
        """
        Cleanup the destruction of a competition.

        This requires removing phases, submissions, and participants. We should try to design
        the models to make the cleanup simpler if we can.
        """
        # Get the competition
        c = webmodels.Competition.objects.get(id=pk)

        # Create a blank response
        response = {}
        if self.request.user == c.creator:
            c.delete()
            response['id'] = pk
        else:
            response['status'] = 403

        return Response(json.dumps(response), content_type="application/json")

    @action(methods=['GET'],permission_classes=[permissions.IsAuthenticated])
    def publish(self, request, pk):
        """
        Publish a competition.
        """
        c = webmodels.Competition.objects.get(id=pk)
        response = {}
        if self.request.user == c.creator:
            c.published = True
            c.save()
            response['id'] = pk
            response['status'] = 200
        else:
            response['status'] = 403
        return Response(json.dumps(response), content_type="application/json")

    @action(methods=['GET'], permission_classes=[permissions.IsAuthenticated])
    def unpublish(self, request, pk):
        """
        Unpublish a competition.
        """
        c = webmodels.Competition.objects.get(id=pk)
        response = {}
        if self.request.user == c.creator:
            c.published = False
            c.save()
            response['id'] = pk
            response['status'] = 200
        else:
            response['status'] = 403
        return Response(json.dumps(response), content_type="application/json")

    @action(permission_classes=[permissions.IsAuthenticated])
    def participate(self, request, pk=None):
        comp = self.get_object()

        # If there is no registration required we just check to make sure they have agreed to the terms and conditions
        # which is done by the javascript before the ajax call, during form validation.
        if comp.has_registration:
            status = webmodels.ParticipantStatus.objects.get(codename=webmodels.ParticipantStatus.PENDING)
        else:
            status = webmodels.ParticipantStatus.objects.get(codename=webmodels.ParticipantStatus.APPROVED)

        p,cr = webmodels.CompetitionParticipant.objects.get_or_create(user=self.request.user,
                                                                      competition=comp,
                                                                      defaults={'status': status, 'reason': None})

        response_data = {
            'result' : 201 if cr else 200,
            'id' : p.id
        }

        return Response(json.dumps(response_data), content_type="application/json")

    def _get_userstatus(self,request,pk=None,participant_id=None):
        comp = self.get_object()
        resp = {}
        status = 200
        try:
            p = webmodels.CompetitionParticipant.objects.get(user=self.request.user,competition=comp)
            resp = {'status': p.status.codename, 'reason': p.reason}
        except ObjectDoesNotExist:
            resp = {'status': None, 'reason': None}
            status = 400
        return Response(resp,status=status)

    @link(permission_classes=[permissions.IsAuthenticated])
    def mystatus(self,request,pk=None):
        return self._get_userstatus(request,pk)

    @action(methods=['POST','PUT'], permission_classes=[permissions.IsAuthenticated])
    def participation_status(self,request,pk=None):
        comp = self.get_object()
        resp = {}
        status = request.DATA['status']
        part = request.DATA['participant_id']
        reason = request.DATA['reason']

        try:
            p = webmodels.CompetitionParticipant.objects.get(competition=comp, pk=part)
            p.status = webmodels.ParticipantStatus.objects.get(codename=status)
            p.reason = reason
            p.save()
            resp = {
                'status': status,
                'participantId': part,
                'reason': reason
                }
        except ObjectDoesNotExist as e:
            resp = {
                'status' : 400
                }

        return Response(json.dumps(resp), content_type="application/json")

    @action(permission_classes=[permissions.IsAuthenticated])
    def info(self,request,*args,**kwargs):
        comp = self.get_object()
        comp.title = request.DATA.get('title')
        comp.description = request.DATA.get('description')
        comp.save()
        return Response({ "data": {
                              "title": comp.title,
                              "description": comp.description,
                              "imageUrl": comp.image.url if comp.image else None },
                          "published": 3 }, status=200)

competition_list =   CompetitionAPIViewSet.as_view({'get':'list','post':'create', 'post': 'participate',})
competition_retrieve =   CompetitionAPIViewSet.as_view({'get':'retrieve','put':'update', 'patch': 'partial_update'})

class CompetitionParticipantAPIViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionParticipantSerial
    queryset = webmodels.CompetitionParticipant.objects.all()

    def get_queryset(self):
        competition_id = self.kwargs.get('competition_id',None)
        return self.queryset.filter(competition__pk=competition_id)

class CompetitionPhaseAPIViewset(viewsets.ModelViewSet):
    serializer_class = serializers.CompetitionPhaseSerial
    queryset = webmodels.Competition.objects.all()

    def get_queryset(self):
        competition_id = self.kwargs.get('pk',None)
        phasenumber = self.kwargs.get('phasenumber',None)
        kw = {}
        if competition_id:
            kw['pk'] = competition_id
        if phasenumber:
            kw['phases__phasenumber'] = phasenumber
        return self.queryset.filter(**kw)

competitionphase_list = CompetitionPhaseAPIViewset.as_view({'get':'list','post':'create'})
competitionphase_retrieve = CompetitionPhaseAPIViewset.as_view({'get':'retrieve',
                                                                'put':'update',
                                                                'patch':'partial_update'})


class CompetitionPageViewSet(viewsets.ModelViewSet):
    ## TODO: Turn the custom logic here into a mixin for other content
    serializer_class = serializers.PageSerial
    content_type = ContentType.objects.get_for_model(webmodels.Competition)
    queryset = webmodels.Page.objects.all()
    _pagecontainer = None
    _pagecontainer_q = None

    def get_queryset(self):
        kw = {}
        if 'competition_id' in self.kwargs:
            kw['container__object_id'] = self.kwargs['competition_id']
            kw['container__content_type'] = self.content_type
        if 'category' in self.kwargs:
            kw['category__codename'] = self.kwargs['category']
        if kw:
            return self.queryset.filter(**kw)
        else:
            return self.queryset

    def dispatch(self,request,*args,**kwargs):
        if 'competition_id' in kwargs:
            self._pagecontainer_q = webmodels.PageContainer.objects.filter(object_id=kwargs['competition_id'],
                                                                           content_type=self.content_type)
        return super(CompetitionPageViewSet,self).dispatch(request,*args,**kwargs)

    @property
    def pagecontainer(self):
        if self._pagecontainer_q is not None and self._pagecontainer is None:
            try:
                self._pagecontainer = self._pagecontainer_q.get()
            except ObjectDoesNotExist:
                self._pagecontainer = None
        return self._pagecontainer

    def new_pagecontainer(self,competition_id):
        try:
            competition=webmodels.Competition.objects.get(pk=competition_id)
        except ObjectDoesNotExist:
            raise Http404
        self._pagecontainer = webmodels.PageContainer.objects.create(object_id=competition_id,
                                                                     content_type=self.content_type)
        return self._pagecontainer

    def get_serializer_context(self):
        ctx = super(CompetitionPageViewSet,self).get_serializer_context()
        if 'competition_id' in self.kwargs:
            ctx.update({'container': self.pagecontainer})
        return ctx

    def create(self,request,*args,**kwargs):
        container = self.pagecontainer
        if not container:
            container = self.new_pagecontainer(self.kwargs.get('competition_id'))
        return  super(CompetitionPageViewSet,self).create(request,*args,**kwargs)

competition_page_list = CompetitionPageViewSet.as_view({'get':'list','post':'create'})
competition_page = CompetitionPageViewSet.as_view({'get':'retrieve','put':'update','patch':'partial_update'})


@permission_classes((permissions.IsAuthenticated,))
class CompetitionSubmissionViewSet(viewsets.ModelViewSet):
    queryset = webmodels.CompetitionSubmission.objects.all()
    serializer_class = serializers.CompetitionSubmissionSerial
    _file = None

    def get_queryset(self):
        return self.queryset.filter(phase__competition__pk=self.kwargs['competition_id'])

    def pre_save(self,obj):
        try:
            obj.participant = webmodels.CompetitionParticipant.objects.filter(
                                competition=self.kwargs['competition_id'], user=self.request.user).get()
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not obj.participant.is_approved:
            raise PermissionDenied()
        for phase in webmodels.CompetitionPhase.objects.filter(competition=self.kwargs['competition_id']):
            if phase.is_active is True:
                break
        if phase is None or phase.is_active is False:
            raise PermissionDenied(detail = 'Competition phase is closed.')
        obj.phase = phase

    def post_save(self,obj,created):
        if created:
            evaluate_submission(obj.pk, obj.phase.is_scoring_only)

    def handle_exception(self, exc):
        if type(exc) is DjangoPermissionDenied:
            exc = PermissionDenied(detail = str(exc))
        return super(CompetitionSubmissionViewSet, self).handle_exception(exc)

    @action(methods=["DELETE"])
    def removeFromLeaderboard(self, request, pk=None, competition_id=None):
        try:
            participant = webmodels.CompetitionParticipant.objects.filter(competition=self.kwargs['competition_id'],
                                                                          user=self.request.user).get()
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not participant.is_approved:
            raise PermissionDenied()
        submission = webmodels.CompetitionSubmission.objects.get(id=pk)
        if submission.phase.is_active is False:
            raise PermissionDenied(detail = 'Competition phase is closed.')
        if submission.participant.user != self.request.user:
            raise ParseError(detail = 'Invalid submission')
        response = dict()
        lb = webmodels.PhaseLeaderBoard.objects.get(phase=submission.phase)
        lbe = webmodels.PhaseLeaderBoardEntry.objects.get(board=lb, result=submission)
        lbe.delete()
        response['status'] = lbe.id
        return Response(response, status=response['status'], content_type="application/json")

    @action(methods=["POST"])
    def addToLeaderboard(self, request, pk=None, competition_id=None):
        try:
            participant = webmodels.CompetitionParticipant.objects.filter(competition=self.kwargs['competition_id'],
                                                                          user=self.request.user).get()
        except ObjectDoesNotExist:
            raise PermissionDenied()
        if not participant.is_approved:
            raise PermissionDenied()
        submission = webmodels.CompetitionSubmission.objects.get(id=pk)
        if submission.phase.is_active is False:
            raise PermissionDenied(detail = 'Competition phase is closed.')
        if submission.participant.user != self.request.user:
            raise ParseError(detail = 'Invalid submission')
        response = dict()
        lb,_ = webmodels.PhaseLeaderBoard.objects.get_or_create(phase=submission.phase)
        # Currently we only allow one submission into the leaderboard although the leaderboard
        # is setup to accept multiple submissions from the same participant.
        entries = webmodels.PhaseLeaderBoardEntry.objects.filter(board=lb, result__participant=participant)
        for entry in entries:
            entry.delete()
        lbe,cr = webmodels.PhaseLeaderBoardEntry.objects.get_or_create(board=lb, result=submission)
        response['status'] = (201 if cr else 200)
        return Response(response, status=response['status'], content_type="application/json")

competition_submission_retrieve = CompetitionSubmissionViewSet.as_view({'get':'retrieve'})
competition_submission_create = CompetitionSubmissionViewSet.as_view({'post':'create'})
competition_submission_leaderboard = CompetitionSubmissionViewSet.as_view(
                                        {'post':'addToLeaderboard', 'delete':'removeFromLeaderboard'})

class LeaderBoardViewSet(viewsets.ModelViewSet):
    serializer_class = serializers.LeaderBoardSerial
    queryset = webmodels.PhaseLeaderBoard.objects.all()

    def get_queryset(self):
        kw = {}
        competition_id = self.kwargs.get('competition_id', None)
        phase_id = self.kwargs.get('phase_id', None)
        if phase_id:
            kw['phase__pk'] = phase_id
        if competition_id:
            kw['phase__competition__pk'] = competition_id
        return self.queryset.filter(**kw)

leaderboard_list = LeaderBoardViewSet.as_view({'get':'list','post':'create'} )
leaderboard_retrieve = LeaderBoardViewSet.as_view( {'get':'retrieve','put':'update','patch':'partial_update'} )

class DefaultContentViewSet(viewsets.ModelViewSet):
    queryset = webmodels.DefaultContentItem.objects.all()
    serializer_class = serializers.DefaultContentSerial

class SubmissionScoreViewSet(viewsets.ModelViewSet):
    queryset = webmodels.CompetitionSubmission.objects.all()
    serializer_class = serializers.CompetitionScoresSerial

    def get_queryset(self):
        kw = {}
        competition_id = self.kwargs.get('competition_id', None)
        phase_id = self.kwargs.get('phase_id', None)
        participant_id = self.kwargs.get('participant_id', None)
        if competition_id:
            kw['submission__phase__competition__pk'] = competition_id
        if phase_id:
            kw['submission__phase__pk'] = phase_id
        if participant_id:
            kw['submission__participant__pk'] = participant_id
        return self.queryset.filter(**kw)

competition_scores_list = SubmissionScoreViewSet.as_view( {'get':'list'} )
