from __future__ import unicode_literals

import re

from django.contrib import admin
from django.utils.translation import ugettext_lazy as _
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy
from django.template.response import TemplateResponse
from django.contrib.admin import helpers
from django.utils.six import BytesIO
from django import forms

from froide.helper.admin_utils import (make_nullfilter, AdminTagAllMixIn,
                                      ForeignKeyFilter, TaggitListFilter)
from froide.helper.widgets import TagAutocompleteWidget
from froide.helper.forms import get_fk_form_class
from froide.helper.email_utils import EmailParser
from froide.helper.document import can_convert_to_pdf

from .models import (FoiRequest, FoiMessage, FoiProject,
        FoiAttachment, FoiEvent, PublicBodySuggestion,
        DeferredMessage, TaggedFoiRequest, RequestDraft)
from .tasks import count_same_foirequests, convert_attachment_task


SUBJECT_REQUEST_ID = re.compile(r' \[#(\d+)\]')


class FoiMessageInline(admin.StackedInline):
    model = FoiMessage
    raw_id_fields = ('request', 'sender_user', 'sender_public_body', 'recipient_public_body')


class FoiRequestAdminForm(forms.ModelForm):
    class Meta:
        model = FoiRequest
        fields = '__all__'
        widgets = {
            'tags': TagAutocompleteWidget(
                autocomplete_url=reverse_lazy('api:request-tags-autocomplete')
            ),
        }


class FoiRequestTagsFilter(TaggitListFilter):
    tag_class = TaggedFoiRequest


class FoiRequestAdmin(admin.ModelAdmin, AdminTagAllMixIn):
    form = FoiRequestAdminForm

    prepopulated_fields = {"slug": ("title",)}
    inlines = [
        FoiMessageInline,
    ]
    list_display = ('title', 'first_message', 'secret_address', 'checked',
        'public_body', 'status',)
    list_filter = ('jurisdiction', 'first_message', 'last_message', 'status',
        'resolution', 'is_foi', 'checked', 'public', 'visibility',
        'is_blocked',
        make_nullfilter('same_as', _('Has same request')),
        ('user', ForeignKeyFilter), ('public_body', ForeignKeyFilter),
        FoiRequestTagsFilter)
    search_fields = ['title', 'description', 'secret_address', 'reference']
    ordering = ('-last_message',)
    date_hierarchy = 'first_message'

    tags_autocomplete_url = reverse_lazy('api:request-tags-autocomplete')

    actions = ['mark_checked', 'mark_not_foi', 'tag_all',
               'mark_same_as', 'remove_from_index', 'confirm_request',
               'set_visible_to_user', 'unpublish']
    raw_id_fields = ('same_as', 'public_body', 'user',)
    save_on_top = True

    def mark_checked(self, request, queryset):
        rows_updated = queryset.update(checked=True)
        self.message_user(request, _("%d request(s) successfully marked as checked." % rows_updated))
    mark_checked.short_description = _("Mark selected requests as checked")

    def mark_not_foi(self, request, queryset):
        rows_updated = queryset.update(is_foi=False)
        self.message_user(request, _("%d request(s) successfully marked as not FoI." % rows_updated))
    mark_not_foi.short_description = _("Mark selected requests as not FoI")

    def mark_same_as(self, request, queryset):
        """
        Mark selected requests as same as the one we are choosing now.

        """
        opts = self.model._meta
        # Check that the user has change permission for the actual model
        if not self.has_change_permission(request):
            raise PermissionDenied

        Form = get_fk_form_class(self.model, 'same_as', self.admin_site)
        # User has already chosen the other req
        if request.POST.get('obj'):
            f = Form(request.POST)
            if f.is_valid():
                req = f.cleaned_data['obj']
                queryset.update(same_as=req)
                count_same_foirequests.delay(req.id)
                self.message_user(request, _("Successfully marked requests as identical."))
                # Return None to display the change list page again.
                return None
        else:
            f = Form()

        context = {
            'opts': opts,
            'queryset': queryset,
            'media': self.media,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'form': f,
            'applabel': opts.app_label
        }

        # Display the confirmation page
        return TemplateResponse(request, 'foirequest/admin_mark_same_as.html',
            context)
    mark_same_as.short_description = _("Mark selected requests as identical to...")

    def remove_from_index(self, request, queryset):
        from haystack import connections as haystack_connections

        for obj in queryset:
            for using in haystack_connections.connections_info.keys():
                backend = haystack_connections[using].get_backend()
                backend.remove(obj)

        self.message_user(request, _("Removed from search index"))
    remove_from_index.short_description = _("Remove from search index")

    def confirm_request(self, request, queryset):
        foireq = queryset[0]
        if foireq.status != 'awaiting_user_confirmation':
            self.message_user(request, _("Request not in correct state!"))
            return None
        self.message_user(request, _("Message send successfully!"))
        FoiRequest.confirmed_request(foireq.user, foireq.pk)
        return None
    confirm_request.short_description = _("Confirm request if unconfirmed")

    def set_visible_to_user(self, request, queryset):
        queryset.update(visibility=1)
        self.message_user(request, _("Selected requests are now only visible to requester."))
    set_visible_to_user.short_description = _("Set only visible to requester")

    def unpublish(self, request, queryset):
        queryset.update(public=False)
        self.message_user(request, _("Selected requests are now unpublished."))
    unpublish.short_description = _("Unpublish")


class FoiAttachmentInline(admin.TabularInline):
    model = FoiAttachment
    raw_id_fields = ('redacted', 'converted')


class FoiMessageAdmin(admin.ModelAdmin):
    save_on_top = True
    list_display = ('subject', 'timestamp', 'sender_email', 'recipient_email',)
    list_filter = ('is_postal', 'is_response', 'sent', 'status',)
    search_fields = ['subject', 'sender_email', 'recipient_email']
    ordering = ('-timestamp',)
    date_hierarchy = 'timestamp'
    exclude = ('original',)
    raw_id_fields = ('request', 'sender_user', 'sender_public_body', 'recipient_public_body')
    inlines = [
        FoiAttachmentInline,
    ]


class FoiAttachmentAdmin(admin.ModelAdmin):
    raw_id_fields = ('belongs_to', 'redacted', 'converted')
    ordering = ('-id',)
    list_display = ('name', 'filetype', 'admin_link_message', 'approved', 'can_approve',)
    list_filter = ('can_approve', 'approved', 'is_redacted', 'is_converted',
                   make_nullfilter('redacted', _('Has redacted version')),
                   make_nullfilter('converted', _('Has converted version'))
    )
    search_fields = ['name']
    actions = ['approve', 'cannot_approve', 'convert']

    def approve(self, request, queryset):
        rows_updated = queryset.update(approved=True)
        self.message_user(request, _("%d attachment(s) successfully approved." % rows_updated))
    approve.short_description = _("Mark selected as approved")

    def cannot_approve(self, request, queryset):
        rows_updated = queryset.update(can_approve=False)
        self.message_user(request, _("%d attachment(s) successfully marked as not approvable." % rows_updated))
    cannot_approve.short_description = _("Mark selected as NOT approvable")

    def convert(self, request, queryset):
        if not queryset:
            return
        count = 0
        for instance in queryset:
            ft = instance.filetype.lower()
            name = instance.name.lower()
            if can_convert_to_pdf(ft, name=name):
                count += 1
                convert_attachment_task.delay(instance.pk)
        self.message_user(request, _("Conversion tasks started: %s") % count)
    convert.short_description = _("Convert to PDF")


class FoiEventAdmin(admin.ModelAdmin):
    list_display = ('event_name', 'request', 'timestamp',)
    list_filter = ('event_name', 'public')
    search_fields = ['request__title', "public_body__name"]
    ordering = ('-timestamp',)
    date_hierarchy = 'timestamp'
    raw_id_fields = ('request', 'user', 'public_body')


class PublicBodySuggestionAdmin(admin.ModelAdmin):
    list_display = ('request', 'public_body', 'user', 'reason',)
    search_fields = ['request', 'reason']
    ordering = ('-timestamp',)
    date_hierarchy = 'timestamp'
    raw_id_fields = ('request', 'public_body', 'user')


class DeferredMessageAdmin(admin.ModelAdmin):
    model = DeferredMessage

    list_filter = (make_nullfilter('request', _('Has request')), 'spam')
    search_fields = ['recipient']
    date_hierarchy = 'timestamp'
    ordering = ('-timestamp',)
    list_display = ('recipient', 'timestamp', 'request', 'spam')
    raw_id_fields = ('request',)
    actions = ['redeliver', 'auto_redeliver']

    save_on_top = True

    def auto_redeliver(self, request, queryset):
        parser = EmailParser()
        for deferred in queryset:
            email = parser.parse(BytesIO(deferred.encoded_mail()))
            match = SUBJECT_REQUEST_ID.search(email['subject'])
            if match is not None:
                try:
                    req = FoiRequest.objects.get(pk=match.group(1))
                    deferred.redeliver(req)
                except FoiRequest.DoesNotExist:
                    continue
    auto_redeliver.short_description = _("Auto-Redeliver based on subject")

    def redeliver(self, request, queryset, auto=False):
        """
        Redeliver undelivered mails

        """
        opts = self.model._meta
        # Check that the user has change permission for the actual model
        if not self.has_change_permission(request):
            raise PermissionDenied

        Form = get_fk_form_class(self.model, 'request', self.admin_site)
        # User has already chosen the other req
        if request.POST.get('obj'):
            f = Form(request.POST)
            if f.is_valid():
                req = f.cleaned_data['obj']
                for deferred in queryset:
                    deferred.redeliver(req)
                self.message_user(request, _("Successfully triggered redelivery."))
                return None
        else:
            f = Form()

        context = {
            'opts': opts,
            'queryset': queryset,
            'media': self.media,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'form': f,
            'applabel': opts.app_label
        }

        # Display the confirmation page
        return TemplateResponse(request, 'foirequest/admin_redeliver.html',
            context)
    redeliver.short_description = _("Redeliver to...")


class FoiProjectAdminForm(forms.ModelForm):
    class Meta:
        model = FoiProject
        fields = '__all__'
        widgets = {
            'tags': TagAutocompleteWidget(
                autocomplete_url=reverse_lazy('api:request-tags-autocomplete')
            ),
        }


class FoiProjectAdmin(admin.ModelAdmin):
    form = FoiRequestAdminForm

    list_display = ('title', 'created', 'user', 'public', 'status',
                    'request_count')
    list_filter = ('public', 'status',)
    search_fields = ['title', 'description', 'reference']
    ordering = ('-last_update',)
    date_hierarchy = 'created'


class RequestDraftAdmin(admin.ModelAdmin):
    list_display = ('save_date', 'user', 'subject',)
    list_filter = ('public', 'full_text')
    search_fields = ['subject', 'user__email']
    ordering = ('-save_date',)
    date_hierarchy = 'save_date'
    raw_id_fields = ('user', 'publicbodies')


admin.site.register(FoiRequest, FoiRequestAdmin)
admin.site.register(FoiMessage, FoiMessageAdmin)
admin.site.register(FoiAttachment, FoiAttachmentAdmin)
admin.site.register(FoiEvent, FoiEventAdmin)
admin.site.register(PublicBodySuggestion, PublicBodySuggestionAdmin)
admin.site.register(DeferredMessage, DeferredMessageAdmin)
admin.site.register(RequestDraft, RequestDraftAdmin)
admin.site.register(FoiProject, FoiProjectAdmin)
