import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.lib.models import Company, JobPost, Resume, CoverLetter


class TestNestedIncludes(TestCase):
    def setUp(self):
        self.client = APIClient()
        User = get_user_model()
        
        # Create users
        self.user1 = User.objects.create_user(username="user1", password="pass")
        self.user2 = User.objects.create_user(username="user2", password="pass")
        
        # Create company
        self.company = Company(name="Test Company")
        self.company.save()
        
        # Create job post
        self.job_post = JobPost(
            title="Software Engineer",
            description="Great job",
            company_id=self.company.id
        )
        self.job_post.save()
        
        # Create resumes
        self.resume1 = Resume(user_id=self.user1.id, title="User1 Resume")
        self.resume1.save()
        
        self.resume2 = Resume(user_id=self.user2.id, title="User2 Resume")
        self.resume2.save()
        
        # Create cover letters
        self.cover_letter1 = CoverLetter(
            content="Cover letter 1",
            user_id=self.user1.id,
            resume_id=self.resume1.id,
            job_post_id=self.job_post.id
        )
        self.cover_letter1.save()
        
        self.cover_letter2 = CoverLetter(
            content="Cover letter 2", 
            user_id=self.user2.id,
            resume_id=self.resume2.id,
            job_post_id=self.job_post.id
        )
        self.cover_letter2.save()

    def test_nested_includes_cover_letter(self):
        """Test nested include paths like job-post.company,resume work correctly"""
        self.client.force_authenticate(user=self.user1)
        
        response = self.client.get(
            f"/api/v1/cover-letters/{self.cover_letter1.id}",
            {"include": "job-post,job-post.company,resume"}
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        # Check main resource
        self.assertEqual(data["data"]["id"], str(self.cover_letter1.id))
        self.assertEqual(data["data"]["type"], "cover-letter")
        
        # Check included resources
        included = data.get("included", [])
        included_types = {item["type"] for item in included}
        included_ids = {(item["type"], item["id"]) for item in included}
        
        # Should have job-post, company, and resume
        self.assertIn("job-post", included_types)
        self.assertIn("company", included_types)
        self.assertIn("resume", included_types)
        
        # Check specific IDs match
        self.assertIn(("job-post", str(self.job_post.id)), included_ids)
        self.assertIn(("company", str(self.company.id)), included_ids)
        self.assertIn(("resume", str(self.resume1.id)), included_ids)
        
        # Should not have duplicates
        type_counts = {}
        for item in included:
            type_counts[item["type"]] = type_counts.get(item["type"], 0) + 1
        
        for count in type_counts.values():
            self.assertEqual(count, 1, "Should not have duplicate resources in included")

    def test_cover_letter_ownership_filtering_in_nested_includes(self):
        """Test that cover-letters in nested includes are filtered by ownership"""
        self.client.force_authenticate(user=self.user1)
        
        # Get the job-post and include its cover-letters
        response = self.client.get(
            f"/api/v1/job-posts/{self.job_post.id}",
            {"include": "cover-letters"}
        )
        
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        
        # Check included cover letters - should only include user1's cover letter
        included = data.get("included", [])
        cover_letters = [item for item in included if item["type"] == "cover-letter"]
        
        self.assertEqual(len(cover_letters), 1)
        self.assertEqual(cover_letters[0]["id"], str(self.cover_letter1.id))
        
        # Should not include user2's cover letter
        cover_letter_ids = {cl["id"] for cl in cover_letters}
        self.assertNotIn(str(self.cover_letter2.id), cover_letter_ids)
