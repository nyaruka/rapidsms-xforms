from django.db import models
from django.contrib.auth.models import User
from rapidsms.models import Connection
import django.dispatch
import re

class XForm(models.Model):
    """
    An XForm, which is just a collection of fields.

    XForms also define their keyword which will be used when submitting via SMS.
    """
    name = models.CharField(max_length=32, unique=True,
                            help_text="Human readable name.")
    keyword = models.SlugField(max_length=32, unique=True,
                               help_text="The SMS keyword for this form, must be a slug.")
    description = models.TextField(max_length=255,
                               help_text="The purpose of this form.")
    response = models.CharField(max_length=255,
                                help_text="The response sent when this form is successfully submitted.")
    active = models.BooleanField(default=True,
                                 help_text="Inactive forms will not accept new submissions.")

    owner = models.ForeignKey(User)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    def update_submission_from_dict(self, submission, values):
        """
        Sets the values for the passed in submission to the passed in dictionary.  The dict
        is expected to have keys corresponding to the commands of the fields.

        Note that the submission will set itself as no longer having any errors and trigger
        the xform_submitted signal

        TODO: I'm kind of putting all real logic in XForm as a base, but maybe this really
        belongs in XFormSubmission where I first had it.
        """
        # first update any existing values, removing them from our dict as we work
        for value in submission.values.all():
            # we have a new value, update it
            if value.field.command in values:
                value.value = values[value.field.command]
                value.save()
                del values[value.field.command]

            # no new value, we need to remove this one
            else:
                value.delete()

        # now add any remaining values in our dict
        for key, value in values.items():
            # look up the field by key
            field = XFormField.objects.get(xform=self, command=key)
            sub_value = submission.values.create(field=field, value=str(value))

        # clear out our error flag if there were some
        if submission.has_errors:
            submission.has_errors = False
            submission.save()

        # trigger our signal for anybody interested in form submissions
        xform_received.send(sender=self, xform=self, submission=submission)

    def process_sms_submission(self, message, connection):
        """
        Given an incoming SMS message, will create a new submission.  If there is an error
        we will throw with the appropriate error message.
        
        The newly created submission object will be returned.
        """

        # sms submissions must be in the form:
        #    <keyword> +field_command1 [values] +field_command2 [values]
        #
        # so first let's just split on +
        segments = message.split('+')

        # ignore everything before the first '+' that is the keyword and/or other data we
        # aren't concerned with
        segments = segments[1:]

        # the errors in this submission
        errors = []

        # create our new submission, we'll add field values as we parse them
        submission = XFormSubmission(xform=self, type='sms', raw=message, connection=connection)
        submission.save()

        # now for each segment
        for segment in segments:
            # grab the command
            command = segment.strip().split(' ')[0].lower()

            # find the corresponding field, check its value and save it if it passes
            for field in self.fields.all():
                if field.command == command:
                    found = True
                    value = ' '.join(segment.strip().split(' ')[1:])

                    error = field.check_value(value)

                    if error:
                        errors.append(error)
                    else:
                        submission.values.create(field=field, value=value)

        # if we had errors
        if errors:
            # stuff them as a transient variable in our submission, our caller may message back
            submission.errors = errors
            
            # and set our db state as well
            submission.has_errors = True
            submission.save()


        # trigger our signal
        xform_received.send(sender=self, xform=self, submission=submission)

        return submission
                        
    def __unicode__(self): # pragma: no cover
        return self.name

TYPE_CHOICES = (
    ('int', 'Integer'),
    ('dec', 'Decimal'),
    ('str', 'String'),
    ('gps', 'GPS Coordinates')
)

class XFormField(models.Model):
    """
    A field within an XForm.  Fields can be one of the types:
        int: An integer
        dec: A decimal or float value
        str: A string
        gps: A lat and long pairing

    Note that when defining a field you must also define it's ``command`` which will
    be used to 'tag' the field in an SMS message.  ie: ``+age 10``

    """

    xform = models.ForeignKey(XForm, related_name='fields')

    type = models.CharField(max_length=3, choices=TYPE_CHOICES)
    command = models.SlugField(max_length=8)
    caption = models.CharField(max_length=16)
    description = models.CharField(max_length=64)
    order = models.IntegerField(default=0)

    def check_value(self, value):
        """
        Checks the passed in string value to see if it matches the constraints that have been set
        on this field.  There are implicit cosntraints created by the type of the field, ie: int
        fields must be integers, and there are explicit constriants created by the user.  Implicit
        constraints are evaluated first before implicit ones.

        The return value is None in the case of no errors or the first error message if one of the
        constraints fails.
        """

        # check against our type first if we have a value

        if value is not None and len(value) > 0:
            if self.type == 'int':
                try:
                    test = int(value)
                except ValueError:
                    return "+%s parameter must be an even number." % self.command

            if self.type == 'dec':
                try:
                    test = float(value)
                except ValueError:
                    return "+%s parameter must be a number." % self.command


            # for gps, we expect values like 1.241 1.543, so basically two numbers
            if self.type == 'gps':
                coords = value.split(' ')
                if len(coords) != 2:
                    return "+%s parameter must be GPS coordinates in the format 'lat long'" % self.command
                for coord in coords:
                    try:
                        test = float(coord)
                    except ValueError:
                        return "+%s parameter must be GPS coordinates the format 'lat long'" % self.command

            # anything goes for strings

        # now check our actual constraints if any
        for constraint in self.constraints.order_by('order'):
            error = constraint.check_value(value)
            if error:
                return error
        
        return None

    def __unicode__(self): # pragma: no cover
        return self.caption

CONSTRAINT_CHOICES = (
    ('min_val', 'Minimum Value'),
    ('max_val', 'Maximum Value'),
    ('min_len', 'Minimum Length'),
    ('max_len', 'Maximum Length'),
    ('req_val', 'Required'),
    ('regex', 'Regular Expression')
)

class XFormFieldConstraint(models.Model):
    """
    Constraint on a field.  A field can have 0..n constraints.  Constraints can be of
    the types:
        req_val: A value is required in every submission, though it can be an empty string
        min_val: The numerical value must be at least n
        max_val: The numerical value must be at most n
        min_len: The length of the value must be at least n
        max_len: The length of the value must be at most n
        regex: The value must match the regular expression

    All constraints also define an error message which will be returned if the constraint fails.

    Constraints are evaluated in order, the first constraint to fail shortcuts all subsequent 
    constraints.
    """


    field = models.ForeignKey(XFormField, related_name='constraints')
    
    type = models.CharField(max_length=10, choices=CONSTRAINT_CHOICES)
    test = models.CharField(max_length=255, null=True)
    message = models.CharField(max_length=100)
    order = models.IntegerField(default=1000)

    def check_value(self, value):
        if self.type == 'req_val':
            if not value:
                return self.message

        # if our value is None, none of these other constraints apply
        if value is None:
            return None

        # otherwise, check the appropriate constraint
        if self.type == 'min_val':
            if float(value) < float(self.test):
                return self.message

        elif self.type == 'max_val':
            if float(value) > float(self.test):
                return self.message

        elif self.type == 'min_len':
            if len(value) < int(self.test):
                return self.message

        elif self.type == 'max_len':
            if len(value) > int(self.test):
                return self.message

        elif self.type == 'regex':
            if not re.match(self.test, value, re.IGNORECASE):
                return self.message

        return None

    def __unicode__(self): # pragma: no cover
        return "%s (%s)" % (self.type, self.value)


SUBMISSION_CHOICES = (
    ('www', 'Web Submission'),
    ('sms', 'SMS Submission'),
    ('odk-www', 'ODK Web Submission'),
    ('odk-sms', 'ODK SMS Submission')
)

class XFormSubmission(models.Model):
    """
    Represents an XForm submission.  This acts as an aggregator for the values and a way of 
    storing where the submission came form.
    """

    xform = models.ForeignKey(XForm, related_name='submissions')
    type = models.CharField(max_length=8, choices=SUBMISSION_CHOICES)
    connection = models.ForeignKey(Connection, null=True)
    raw = models.TextField()
    has_errors = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    # transient, only populated when the submission first comes in
    errors = []

    def __unicode__(self): # pragma: no cover
        return "%s (%s)" % (self.xform, self.type)


class XFormSubmissionValue(models.Model):
    """
    Stores a value for a field that was submitted.  Note that this is a rather inelegant
    representation of the data, in that nothing is typed.  This is by design.  It isn't
    the job of XForms to store your cannonical version of the data, only to allow easy
    collection and validation.
    """

    submission = models.ForeignKey(XFormSubmission, related_name='values')
    field = models.ForeignKey(XFormField, related_name="submission_values")
    value = models.CharField(max_length=255)

    def __unicode__(self): # pragma: no cover
        return "%s=%s" % (self.field, self.value)


# Signal triggered whenever an xform is received.  The caller can derive from the submission
# whether it was successfully parsed or not and do what they like with it.

xform_received = django.dispatch.Signal(providing_args=["xform", "submission"])
